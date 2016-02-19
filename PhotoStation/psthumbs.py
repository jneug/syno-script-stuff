#!/usr/bin/env python
# Generates thumbnails for the Snyology Photo Station
# Based on the synothumb script by phillips321 (http://www.phillips321.co.uk)
#    - https://github.com/phillips321/synothumbs
#
# Tested on Mac OS X. Mount the synology photos folder by nfs with
# ```
# sudo mount_nfs -P 192.168.1.55:/volume1/photo ~/photo
# ```
#
# TODO: mount share and use rsync to sync files to share at the end
# TODO: add -f option to force generation of thumbnails
# TODO: add -v option = verbose mode
import time
import argparse
import os
from queue import Queue
import threading
import subprocess
import shlex

from PIL import Image, ImageChops
from io import StringIO, BytesIO

VERSION = '0.1.0'
START_TIME = time.time()  # Start time for measurements

###############################################################################
# Default Settings                                                            #
###############################################################################
# only lowercase extensions!
IMAGE_EXTENSIONS = ['.jpg', '.png', '.jpeg',
                    '.tif', '.bmp']  # Image types to be handled by PIL
RAW_EXTENSIONS = ['.arw']  # Raw images types to convert with dcraw
VIDEO_EXTENSIONS = ['.mov', '.m4v', 'mp4']  # Video types to handlewith ffmpeg

IGNORED_FILES = [".DS_Store", ".apdisk", "Thumbs.db"]

# Synology thumbnail folder
THUMB_DIR = '@eaDir'
# Synology thumbnail sizes (fit to size), descending order
THUMB_SIZES = [
    ('SYNOPHOTO_THUMB_XL.jpg', (1280, 1280)),   # 0: XtraLarge
    ('SYNOPHOTO_THUMB_L.jpg', (800, 800)),      # 1: Large
    ('SYNOPHOTO_THUMB_B.jpg', (640, 640)),      # 2: Big
    ('SYNOPHOTO_THUMB_M.jpg', (320, 320)),      # 3: Medium
    ('SYNOPHOTO_THUMB_S.jpg', (160, 160))       # 4: Small
]
# Synology preview size (keep ratio, pad with black)
PREVIEW_SIZE = ('SYNOPHOTO_THUMB_PREVIEW.jpg', (120, 160))


###############################################################################
# CLI arguments                                                               #
###############################################################################
parser = argparse.ArgumentParser(
    description='Generate thumbnails for Synologys Photo Station.'
)
parser.add_argument('rootdir', metavar='DIR',
                    help='the root directory to search for ' +
                         'images to generate thumbnails of')
parser.add_argument('-f', '--force', dest='force', action='store_true',
                    help='force the generation of thumbnails, ' +
                         'overwrite existing files')
parser.add_argument('-s', '--nfs', metavar='URL', dest='nfs_share',
                    help='location of a nfs share to connect to and ' +
                         'sync the files to')
parser.add_argument('-r', '--rsync', metavar='OPTIONS', dest='rsync_options',
                    choices=['t', 'p', 'tp'], default='tp',
                    help='determines what to sync to the nfs share: ' +
                         '[t]humbnails only, [p]hotos only or both [tp], ' +
                         'requires -n to connect to a nfs share first')
parser.add_argument('-v', '--verbose', dest='verbose', action='store_true',
                    help='enable verbose output')
parser.add_argument('-t', '--threads', metavar='N', dest='num_of_threads',
                    type=int, default=4,
                    help='number of threads to use')
parser.add_argument('--version', action='version',
                    version='%(prog)s ' + VERSION)


###############################################################################
# Media handling functions                                                    #
###############################################################################
media_queue = Queue()  # Initialize global media queue
failed_files = []


def media_queue_builder(rootdir):
    # check availalbe tools and appropriate extensions
    allExtensions = [] + IMAGE_EXTENSIONS
    if _is_tool('dcraw'):
        allExtensions += RAW_EXTENSIONS
    else:
        print('[+] dcraw not available in PATH, can\'t convert raw images')
    if _is_tool('ffmpeg'):
        allExtensions += VIDEO_EXTENSIONS
    else:
        print('[+] ffmpeg not available in PATH, can\'t generate previews ' +
              'for videos')

    # find files and put into queue
    for path, subFolders, files in os.walk(rootdir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in allExtensions:  # is a viable file type?
                if THUMB_DIR not in path:
                    if file not in IGNORED_FILES:  # maybe remove (?)
                        media_queue.put(os.path.join(path, file))
                        print("[+] Added %s to queue" % os.path.join(path,
                              file))


def _is_tool(name):
    """
    Checks if the binary for the tool with the given name is available
    to call.
    """
    try:
        devnull = open(os.devnull)
        subprocess.Popen([name], stdout=devnull,
                         stderr=devnull).communicate()
    except OSError as e:
        if e.errno == os.errno.ENOENT:
            return False
    return True


def media_queue_worker():
    while True:
        media_item = media_queue.get()
        if media_item is None:
            break
        media_converter(media_item)
        media_queue.task_done()


def media_converter(file):
    print("[-] Working on %s" % file)

    ext = os.path.splitext(file)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        _image_converter(file)
    elif ext in RAW_EXTENSIONS:
        _raw_converter(file)
    elif ext in VIDEO_EXTENSIONS:
        _video_converter(file)


def _image_converter(file):
    """
    Generats thumbnails for image files (with an extension in
    IMAGE_EXTENSIONS).
    """
    try:
        _generate_thumbnails(file, Image.open(file))
    except OSError as e:
        failed_files.append(file)
        print("[-] Failed to read image %s" % file)
        print("[-] Exception: %s" % e.strerror)


def _raw_converter(file):
    """
    Converts raw images (with an extension in RAW_EXTENSIONS) with dcraw
    and generates thumbnails. The command used for all raw files is
    `dcraw -c -b 8 -q 0 -w -H 5 '%s'`.
    """
    print("[-] Converting raw image %s" % file)
    try:
        dcraw_cmd = "dcraw -c -b 8 -q 0 -w -H 5 '%s'" % file
        dcraw_proc = subprocess.Popen(
            shlex.split(dcraw_cmd), stdout=subprocess.PIPE)
        image_raw = BytesIO(dcraw_proc.communicate()[0])
        _generate_thumbnails(file, Image.open(image_raw))
    except Exception as e:
        failed_files.append(file)
        print("[-] Failed to convert raw image %s" % file)
        print("[-] Exception: %s" % e.message)


def _generate_thumbnails(file, image):
    """
    Generates thumbnail and preview files for the provided image.
    """
    thumbs_dir = _make_thumbs_dir(file)

    # Generate thumbnails in all sizes by consecutively shrinking
    # the original image
    for thumb_name, size in THUMB_SIZES:
        image.thumbnail(size, Image.ANTIALIAS)
        image.save(os.path.join(thumbs_dir, thumb_name), quality=90)

    # Generate the preview image
    preview_name, preview_size = PREVIEW_SIZE
    image.thumbnail(preview_size, Image.ANTIALIAS)
    # pad out image and save preview image
    image_size = image.size
    preview_img = image.crop((0, 0, preview_size[0], preview_size[1]))
    offset_x = int(max((preview_size[0] - image_size[0]) / 2, 0))
    offset_y = int(max((preview_size[1] - image_size[1]) / 2, 0))
    preview_img = ImageChops.offset(preview_img, offset_x, offset_y)
    preview_img.save(os.path.join(thumbs_dir, preview_name), quality=90)


def _video_converter(file):
    """
    Generats video previews for video files (with an extension in
    VIDEO_EXTENSIONS).
    """
    pass


def _make_thumbs_dir(file):
    """
    Check if the thumbnail directory for the given media item exists
    and create it otherwise. Throws an exception if the directory could not
    be created.
    """
    file_dir, file_name = os.path.split(file)
    thumbs_dir = os.path.join(file_dir, THUMB_DIR, file_name)
    if not os.path.isdir(thumbs_dir):
        print("[-] Creating thumbnail directory at %s" % thumbs_dir)
        os.makedirs(thumbs_dir, exist_ok=True)
    return thumbs_dir


###############################################################################
# Main                                                                        #
###############################################################################
def main():
    cfg = parser.parse_args()

    # Start worker threads
    print("[+] Starting worker threads..")
    threads = []
    for i in range(cfg.num_of_threads):
        t = threading.Thread(target=media_queue_worker)
        t.start()
        threads.append(t)

    # Populate media queue and wait for conversions to finish
    print("[+] Looking for media and populating queue..")
    media_queue_builder(cfg.rootdir)
    media_queue.join()
    print("[+] All media processed..")

    # Stop workers threads
    print("[+] Terminating worker threads..")
    for i in range(cfg.num_of_threads):
        media_queue.put(None)
    for t in threads:
        t.join()

    print("[+] Thumbnail generation completed in %i seconds" % (time.time() -
          START_TIME))


if __name__ == '__main__':
    main()
