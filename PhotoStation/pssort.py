#!/usr/bin/env python3
"""
Sort order:

1. Default:
    Year/Monthno Montname/CreateDate_Filename.Ext
    e.g.: 2015/09 September/2015-09-28_Birthday.jpg
2. with "album:Albumname" Keyword:
    Year/Monthno Albumname/CreateDate_Filename.Ext
    e.g.: 2015/09 My Birthday/2015-09-28_Birthday.jpg
3. with "album:Albumname" and "hazel:no month" Keyword:
    Year/Albumname/CreateDate_Filename.Ext
    e.g.: 2015/My Birthday/2015-09-28_Birthday.jpg
3. with "album:Albumname" and "parent:Parentname" Keyword:
    Parentname/Monthno Albumname/CreateDate_Filename.Ext
    e.g.: Birthdays/09 My Birthday/2015-09-28_Birthday.jpg
3. with "album:Albumname" and "parent:Parentname" and "hazel:no month" Keyword:
    Parentname/Albumname/CreateDate_Filename.Ext
    e.g.: Birthdays/My Birthday 2015/2015-09-28_Birthday.jpg
"""
import argparse
import os
from multiprocessing import Pool
from subprocess import getoutput, call
import shlex
from termcolor import colored
from datetime import datetime
from re import match
import locale

VERSION = '0.1.0'

###############################################################################
# CLI arguments                                                               #
###############################################################################
parser = argparse.ArgumentParser(
    description='Sort and rename musc files in a folder based on ' +
                'their metadata.'
)
parser.add_argument('rootdir', metavar='PATH',
                    help='either a folder to search for music files ' +
                         'or a single file to rename')
parser.add_argument('-o', '--out', metavar='DIR', dest='outdir',
                    help='a directory to sort the renamed files into, ' +
                         'otherwise PATH is used (if a folder) or the ' +
                         'parent of PATH (if a file)')
parser.add_argument('-t', '--threads', metavar='N', dest='num_of_threads',
                    type=int, default=4,
                    help='number of threads to use, default: 4')
parser.add_argument('-r', '--recurse', dest='recurse', action='store_true',
                    help='recurse into subfolders of PATH (if a folder)')
parser.add_argument('-rm', '--squash', dest='squash', action='store_true',
                    help='delete empty folders after sorting (requires -r)')
parser.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                    help='enable verbose output')
parser.add_argument('--version', action='version',
                    version='%(prog)s ' + VERSION)
cfg = parser.parse_args()

locale.setlocale(locale.LC_ALL, 'de_DE')  # TODO: Make argument?


###############################################################################
# Functions                                                                   #
###############################################################################
def log(msg, file=None, thread='+', verbose=False):
    global cfg

    if file is not None:
        msg = msg % colored(file, 'blue')
    msg = '[%s]' % colored(thread, 'green') + ' ' + msg
    if (not verbose) or cfg.verbose:
        print(msg)


def vlog(msg, file=None, thread='+'):
    log(msg, file=file, thread=thread, verbose=True)


def sanitize(string, keepcharacters=(' ', '.', '_')):
    """http://stackoverflow.com/a/7406369"""
    string = str(string)
    string = "".join(c for c in string if
                     c.isalnum() or c in keepcharacters)
    # string = "_".join(string.split())
    string = " ".join(string.split())
    return string


def get_file_tags(file, tags, empty=None):
    """
    Run `exiftool` as a subprocess and return the specified list of tags
    as a dictionary. Not set tags will be returned as `None`.
    """
    args = ' '.join(['-%s' % t for t in tags])
    exiftool_cmd = 'exiftool -s3 -f %s "%s"' % (args,
                                                file.replace('"', '\\\"'))
    exiftool_result = getoutput(exiftool_cmd)

    result = {}
    if not exiftool_result.startswith('File not found'):
        for i, line in enumerate(exiftool_result.split('\n')):
            if line == '-':
                result[tags[i]] = empty
            else:
                result[tags[i]] = line

    # Add missing keys..
    for t in tags:
        if t not in result:
            result[t] = None

    return result


def update_create_date(file):
    """
    Run `exiftool` to set the files creation date to the one read from
    the exif timestamp.
    """
    exiftool_cmd = 'exiftool -FileModifyDate<CreateDate ' \
                   '"%s"' % file.replace('"', '\\\"')
    call(shlex.split(exiftool_cmd))


def get_create_date(file, tags):
    if not tags['CreateDate']:
        return datetime.fromtimestamp(os.path.getctime(file))
    else:
        return datetime.strptime(tags['CreateDate'], '%Y:%m:%d %H:%M:%S')


def dir_walker(rootdir, recurse=False):
    if recurse:
        for root, dirs, files in os.walk(rootdir):
            for name in files:
                if not name.startswith('.'):
                    yield os.path.join(root, name)
    else:
        for file in os.scandir(rootdir):
            if file.is_file():
                if not file.name.startswith('.'):
                    yield file.path


def pool_init():
    pass


def file_processor(filename):
    vlog('Working on %s..', file=filename)
    # update_create_date(filename)

    # Get exif tags and sanitize them
    tags = get_file_tags(filename, ['CreateDate', 'Keywords'])
    tags['CreateDate'] = get_create_date(filename, tags)
    if tags['Keywords'] is not None:
        tags['Keywords'] = [x.strip() for x in
                            tags['Keywords'].split(',')]
    else:
        tags['Keywords'] = []

    new_path = get_new_path(filename, tags)
    new_name = get_new_name(filename, tags)

    if cfg.outdir is not None and os.path.isdir(cfg.outdir):
        full_path = os.path.join(cfg.outdir, new_path)
    else:
        full_path = os.path.join(cfg.rootdir, new_path)

    os.makedirs(full_path, exist_ok=True)
    os.rename(filename, os.path.join(full_path, new_name))

    vlog("New filename: %s", file=os.path.join(full_path, new_name))


def get_new_path(file, tags):
    parentname = tags['CreateDate'].strftime('%Y')
    for keyword in tags['Keywords']:
        if keyword.startswith('parent:'):
            parentname = keyword[7:]
            break

    albumname = tags['CreateDate'].strftime('%B')
    for keyword in tags['Keywords']:
        if keyword.startswith('album:'):
            albumname = keyword[6:]
            if match(r'\d{4} - ', albumname):
                albumname = albumname[7:]
            break

    if 'hazel:no month' not in tags['Keywords']:
        albumname = '%s %s' % (tags['CreateDate'].strftime('%m'), albumname)

    return os.path.join(sanitize(parentname), sanitize(albumname))


def get_new_name(file, tags):
    filename = os.path.split(file)[1]
    if not match(r'\d{4}-\d{2}-\d{2}_', filename):
        return '%s_%s' % (tags['CreateDate'].date().isoformat(), filename)
    else:
        return filename


###############################################################################
# Main                                                                        #
###############################################################################
if __name__ == '__main__':
    log('Sorting files..')

    with Pool(processes=cfg.num_of_threads,
              initializer=pool_init) as pool:
        pool.map(file_processor, dir_walker(cfg.rootdir, recurse=cfg.recurse))

    if os.path.isdir(cfg.rootdir) and cfg.recurse and cfg.squash:
        log('Looking for empty folders to squash..')
        for path, subFolders, files in os.walk(cfg.rootdir, topdown=False):
            for dir in subFolders:
                if not os.listdir(os.path.join(path, dir)):
                    os.rmdir(os.path.join(path, dir))
                    vlog('Removed %s', file=os.path.join(path, dir))
