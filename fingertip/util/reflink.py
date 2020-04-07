# Licensed under GNU General Public License v3 or later, see COPYING.
# Copyright (c) 2019 Red Hat, Inc., see CONTRIBUTORS.

"""
Helper functions and constants for fingertip: reflinking (CoW-powered copying).
"""
# TODO: clean up even more?

import os
import sys
import shutil
import subprocess

from fingertip.util import log, temp
from fingertip.util.path import MACHINES, CACHE


SETUP = os.getenv('FINGERTIP_SETUP', 'suggest')
SIZE = os.getenv('FINGERTIP_SETUP_SIZE', '25G')


def always(src, dst):
    subprocess.run(['cp', '--reflink=always', src, dst], check=True)


def auto(src, dst):
    subprocess.run(['cp', '--reflink=auto', src, dst], check=True)


def is_supported(dirpath):
    tmp = temp.disappearing_file(dstdir=dirpath)
    r = subprocess.Popen(['cp', '--reflink=always', tmp, tmp + '-reflink'],
                         stderr=subprocess.PIPE)
    _, err = r.communicate()
    r.wait()
    temp.remove(tmp, tmp + '-reflink')
    sure_not = b'failed to clone' in err and b'Operation not supported' in err
    if r.returncode and not sure_not:
        log.error('reflink support detection inconclusive, cache dir problems')
    return r.returncode == 0


def create_supported_fs(backing_file, size):
    subprocess.run(['fallocate', '-l', size, backing_file], check=True)
    subprocess.run(['mkfs.xfs', '-m', 'reflink=1', backing_file],
                   check=True)


def mount_supported_fs(backing_file, tgt):
    log.info('mounting a reflink-supported filesystem for image storage...')
    tgt_uid, tgt_gid = os.stat(tgt).st_uid, os.stat(tgt).st_gid
    subprocess.run(['sudo', 'mount', '-o', 'loop', backing_file, tgt],
                   check=True)
    mount_uid, mount_gid = os.stat(tgt).st_uid, os.stat(tgt).st_gid
    if (tgt_uid, tgt_gid) != (mount_uid, mount_gid):
        log.debug(f'fixing owner:group ({tgt_uid}:{tgt_gid})')
        subprocess.run(['sudo', 'chown', f'{tgt_uid}:{tgt_gid}', tgt],
                       check=True)


def storage_setup_wizard():
    assert SETUP in ('auto', 'suggest', 'never')
    if SETUP == 'never':
        return
    size = SIZE
    machinesdir = MACHINES
    if not os.path.exists(machinesdir):
        os.mkdir(machinesdir)
    if not is_supported(machinesdir):
        log.warning(f'images directory {machinesdir} lacks reflink support')
        log.warning('without it, fingertip will thrash and fill up your SSD in no time')
        backing_file = os.path.join(CACHE, 'for-machines.xfs')
        if not os.path.exists(backing_file):
            if SETUP == 'suggest':
                log.info(f'would you like to allow fingertip to allocate {size} '
                         f'at {backing_file} '
                         'for a reflink-enabled XFS loop mount?')
                log.info('(set FINGERTIP_SETUP="auto" environment variable'
                         ' to do it automatically)')
                i = input(f'[{size}]/different size/cancel/ignore> ').strip()
                if i == 'cancel':
                    log.error('cancelled')
                    sys.exit(1)
                elif i == 'ignore':
                    return
                size = i or size
            tmp = temp.disappearing_file(CACHE)
            create_supported_fs(tmp, size)
            os.rename(tmp, backing_file)

        log.info(f'fingertip will now mount the XFS image at {backing_file}')
        if SETUP == 'suggest':
            i = input(f'[ok]/skip/cancel> ').strip()
            if i == 'skip':
                log.warning('skipping; fingertip will have no reflink superpowers')
                log.warning('tell your SSD I\'m sorry')
                return
            elif i and i != 'ok':
                log.error('cancelled')
                sys.exit(1)

        mount_supported_fs(backing_file, MACHINES)

    # Schedule automatic cleanup
    storage_schedule_cleanup()


def storage_unmount():
    log.plain()
    log.info(f'unmounting {MACHINES} ...')
    subprocess.run(['sudo', 'umount', '-l', MACHINES])
    log.nicer()


def storage_schedule_cleanup():
    # Do this only if fingertip is in PATH
    if not shutil.which("fingertip"):
        log.debug('No `fingertip` found in PATH. Not scheduling '
                    'automatic cleanup.')
        return

    # Skip if systemd is not available
    if not shutil.which('systemd-run') or not shutil.which('systemctl'):
        log.warning('It looks like systemd is not available. '
                    'No cleanup is scheduled! If you are running out of disk, '
                    'space, run `fingertip cleanup periodic` manually.')
        return

    # If the timer is already installed skip installation too
    try:
        subprocess.run(['systemctl', '--user', 'is-active', '--quiet',
                       'fingertip-cleanup.timer'])
        # exit code 0:
        log.debug('The systemd timer handling cleanup is already installed '
                  'and running.')
        return
    except CalledProcessError as e:
        # non-zero exit code means it is not active or not existing
        pass

    # Run twice a day
    log.info('Scheduling cleanup twice a day at 8AM and 8PM')
    subprocess.run(['systemd-run', '--unit=fingertip-cleanup', '--user',
                    '--on-calendar=', '*-*-* 08,20:00:00',
                    'fingertip', 'cleanup', 'periodic'])
