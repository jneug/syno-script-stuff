#!/usr/bin/env python3
# rsync -au --iconv=UTF8-MAC,UTF-8 --prune-empty-dirs ./Music\ Library/ /Volumes/music/ --progress --include=*.mp3 --include=*.m4a
import argparse
import os
import threading
from queue import Queue
from subprocess import getoutput
from termcolor import colored

VERSION = '0.2.1'

###############################################################################
# Default Settings                                                            #
###############################################################################
# only lowercase extensions!
EXTENSIONS = ['.mp3', '.m4a']

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


###############################################################################
# Functions                                                                   #
###############################################################################
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


def get_new_path(file, tags):
    artist = 'Unknown'
    album = 'Unknown'

    if tags['Band'] is not None:
        artist = tags['Band']
    elif tags['Compilation'] == '1' or tags['Compilation'] == 'Yes':
        artist = 'Compilation'
    elif tags['Artist'] is not None:
        artist = tags['Artist']

    if tags['Album'] is not None:
        album = tags['Album']

    letter = '#'
    _artist = [x for x in artist.lower().replace('the', '') if x.isalnum()]
    if _artist:
        letter = _artist[0].upper()

    new_path = os.path.join(letter,
                            sanitize(artist),
                            sanitize(album))
    return new_path


def get_new_name(file, tags):
    title = tags['Title']

    track = tags['Track']
    if track is None and tags['TrackNumber'] is not None:
        track = tags['TrackNumber'].replace(' of ', '/')

    partofset = tags['PartOfSet']
    if partofset is None and tags['DiscNumber'] is not None:
        partofset = tags['DiscNumber'].replace(' of ', '/')

    if track is not None:
        track = track.split('/')[0].zfill(2)
        if tags['PartOfSet'] is not None:
            partofset = partofset.split('/')
            if len(partofset) == 1 and int(partofset[0]) > 1:
                track = '%s-%s' % (partofset[0], track)
            elif int(partofset[0]) > 1 and int(partofset[1]) > 1:
                track = '%s-%s' % (partofset[0], track)
    else:
        track = '00'

    ext = os.path.splitext(file)[1].lower()

    new_name = '%s %s%s' % (track, sanitize(title), ext)
    return new_name


def music_worker():
    global cfg

    while True:
        media_item = MEDIA_QUEUE.get()
        if media_item is None:
            break

        vlog('Working on %s', file=media_item,
             thread=threading.current_thread().getName())
        file_tags = get_file_tags(media_item, tags)

        try:
            new_path = get_new_path(media_item, file_tags)
            new_name = get_new_name(media_item, file_tags)

            if cfg.outdir is not None and os.path.isdir(cfg.outdir):
                full_path = os.path.join(cfg.outdir, new_path)
            else:
                full_path = os.path.join(cfg.rootdir, new_path)

            os.makedirs(full_path, exist_ok=True)
            os.rename(media_item, os.path.join(full_path, new_name))

            vlog('Moved file to %s',
                 file=os.path.join(full_path, new_name),
                 thread=threading.current_thread().getName())
        except OSError as e:
            print('%s' % e)
        except Exception as e2:
            print('%s' % e)
        finally:
            MEDIA_QUEUE.task_done()
            log('Finished working on %s', file=media_item,
                thread=threading.current_thread().getName())


MEDIA_QUEUE = Queue()


###############################################################################
# Main                                                                        #
###############################################################################
if __name__ == '__main__':
    tags = ['Artist', 'Band',
            'Album', 'Title',
            'Track', 'TrackNumber',
            'PartOfSet', 'DiscNumber',
            'Compilation']

    # Start worker threads
    vlog("Starting worker threads..")
    threads = []
    for i in range(cfg.num_of_threads):
        t = threading.Thread(target=music_worker)
        t.setDaemon(True)
        t.start()
        threads.append(t)

    # Walk file tree and add files to work on
    if os.path.isdir(cfg.rootdir):
        log("Looking for music and populating queue..")
        n = 0
        if cfg.recurse:
            for path, subFolders, files in os.walk(cfg.rootdir):
                for file in files:
                    ext = os.path.splitext(file)[1].lower()
                    if ext in EXTENSIONS:  # is a viable file type?
                        MEDIA_QUEUE.put(os.path.join(path, file))
                        n += 1
        else:
            for file in os.scandir(cfg.rootdir):
                if file.is_file():
                    ext = os.path.splitext(file.name)[1].lower()
                    if ext in EXTENSIONS:  # is a viable file type?
                        MEDIA_QUEUE.put(file.path)
                        n += 1
        log('Found %i music files. Processing..' % n)
    else:
        ext = os.path.splitext(cfg.rootdir)[1].lower()
        if ext in EXTENSIONS:  # is a viable file type?
            MEDIA_QUEUE.put(cfg.rootdir)
            log('Processing 1 file..' % n)
    MEDIA_QUEUE.join()
    log("All music processed.")

    # Stop workers threads
    vlog("Terminating worker threads..")
    for i in range(cfg.num_of_threads):
        MEDIA_QUEUE.put(None)
    for t in threads:
        t.join()

    if os.path.isdir(cfg.rootdir) and cfg.recurse and cfg.squash:
        log('Looking for empty folders to squash..')
        for path, subFolders, files in os.walk(cfg.rootdir, topdown=False):
            for dir in subFolders:
                if not os.listdir(os.path.join(path, dir)):
                    os.rmdir(os.path.join(path, dir))
                    vlog('Removed %s', file=os.path.join(path, dir))
