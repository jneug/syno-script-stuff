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
# TODO: Replace PIl with Pillow ?
# TODO: Replaxe threading/Queue with multiprocessing ?
#       See https://github.com/mbrrg/synology-thumbgen/blob/master/psthumbgen.py
import time
import argparse
import os
from queue import Queue
import threading
import subprocess
import shlex

from PIL import Image, ImageChops, ImageFile
from io import StringIO, BytesIO

VERSION = '0.2.0'
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
# See http://www.web3.lu/managing-thumbnails-synology-photostation/
THUMB_SIZES = (
    ('SYNOPHOTO_THUMB_XL.jpg', (1280, 1280)),   # 0: XtraLarge
    ('SYNOPHOTO_THUMB_L.jpg', (800, 800)),      # 1: Large
    ('SYNOPHOTO_THUMB_B.jpg', (640, 640)),      # 2: Big
    ('SYNOPHOTO_THUMB_M.jpg', (320, 320)),      # 3: Medium
    ('SYNOPHOTO_THUMB_S.jpg', (160, 160))       # 4: Small
)
# Synology preview size (keep ratio, pad with black)
PREVIEW_SIZE = ('SYNOPHOTO_THUMB_PREVIEW.jpg', (120, 160))
# Synology thumbnail sizes (fit to size) for videos, descending order
THUMB_SIZES_VIDEO = (
    THUMB_SIZES[0],
    THUMB_SIZES[3]
)
PREVIEW_SIZE_VIDEO = ('SYNOPHOTO:FILM.flv', (320, 180))


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
                    help='number of threads to use, default: 4')
parser.add_argument('-vd', metavar='S', dest='video_duration',
                    type=int, default=30,
                    help='maximum duration of generated preview videos, ' +
                         'default: 30')
parser.add_argument('-vt', metavar='HH:MM:SS', dest='video_timecode',
                    default='00:00:03',
                    help='timecode for the frame to use for generating ' +
                         'video thumbnails, default: 00:00:03')
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
    image = Image.open(file)
    try:
        image.load()
        _generate_thumbnails(file, image)
    except OSError as e:
        try:
            ImageFile.LOAD_TRUNCATED_IMAGES = True
            _generate_thumbnails(file, image)
            ImageFile.LOAD_TRUNCATED_IMAGES = False
        except OSError as e2:
            failed_files.append(file)
            print("[X] Failed to read image %s" % file)
            print("[X] Exception: %s" % e2)


def _raw_converter(file):
    """
    Converts raw images (with an extension in RAW_EXTENSIONS) with dcraw
    and generates thumbnails. The command used for all raw files is
    `dcraw -c -b 8 -q 0 -w -H 5 '%s'`.
    """
    print("[-] Converting raw image %s" % file)
    try:
        dcraw_cmd = "dcraw -c -b 8 -q 0 -w -H 5 '%s'" % file
        dcraw_proc = subprocess.Popen(shlex.split(dcraw_cmd),
                                      stdout=subprocess.PIPE)
        image_raw = BytesIO(dcraw_proc.communicate()[0])
        _generate_thumbnails(file, Image.open(image_raw))
    except Exception as e:
        failed_files.append(file)
        print("[X] Failed to convert raw image %s" % file)
        print("[X] Exception: %s" % e.message)


def _generate_thumbnails(file, image):
    """
    Generates thumbnail and preview files for the provided image.
    """
    thumbs_dir = None
    try:
        thumbs_dir = _make_thumbs_dir(file)
    except:  # Failed to generate thumbs dir (other exceptions possible?)
        print("[X] Failed to create thumbnail directory for %s" % file)
        failed_files.append(file)
        return

    _rotate_image(image)

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


def _rotate_image(image):
    """
    Attempts to rotate the provided image according to the EXIF information
    found in the image.
    """
    try:
        # code adapted from:
        # http://www.lifl.fr/~riquetd/auto-rotating-pictures-using-pil.html
        exif = image._getexif()
        if exif:
            orientation_key = 274  # cf ExifTags
            if orientation_key in exif:
                orientation = exif[orientation_key]

                rotate_values = {3: 180, 6: 270, 8: 90}

                if orientation in rotate_values:
                    image = image.rotate(rotate_values[orientation])
    except:
        pass  # could not rotate image, proceed with image as is


def _video_converter(file):
    """
    Generats video previews for video files (with an extension in
    VIDEO_EXTENSIONS).
    """
    thumbs_dir = None
    try:
        thumbs_dir = _make_thumbs_dir(file)
    except:  # Failed to generate thumbs dir (other exceptions possible?)
        print("[X] Failed to create thumbnail directory for %s" % file)
        failed_files.append(file)
        return

    # Generate .flv preview video
    ffmpeg_cmd = 'ffmpeg -loglevel panic -i "%s" -y -ar 44100 -r 12 ' \
                 '-ac 2 -f flv -qscale 5 -s 320x180 -aspect 320:180 -t 30 ' \
                 '"%s/%s"' % (file, thumbs_dir, PREVIEW_SIZE_VIDEO[0])
    # TODO: replace -s and -aspect with PREVIEW_SIZE_VIDEO[1]
    # TODO: cut preview to x seconds? Possible options:
    #       -fs limit_size (output)
    #           Set the file size limit, expressed in bytes. No further chunk
    #           of bytes is written after the limit is exceeded. The size of
    #           the output file is slightly more than the requested file size.
    #       -t duration (input/output)
    #           When used as an input option (before -i), limit the duration
    #           of data read from the input file.
    #           When used as an output option (before an output filename),
    #           stop writing the output after its duration reaches duration.
    # TODO: make -t an option

    ffmpeg_proc = subprocess.Popen(shlex.split(ffmpeg_cmd),
                                   stdout=subprocess.PIPE)
    ffmpeg_proc.communicate()[0]

    # Generate temporary preview image
    file_dir, file_name = os.path.split(file)
    thumb_temp = os.path.join('/tmp', os.path.splitext(file_name)[0] + ".jpg")
    ffmpeg_thumb_cmd = 'ffmpeg -loglevel panic -i "%s" -y -an -ss 00:00:03 ' \
                       '-an -r 1 -vframes 1 "%s"' % (file, thumb_temp)
    # TODO: make -ss timecode an option

    ffmpeg_thumb_proc = subprocess.Popen(shlex.split(ffmpeg_thumb_cmd),
                                         stdout=subprocess.PIPE)
    ffmpeg_thumb_proc.communicate()[0]

    # Generate thumbnails in all sizes by consecutively shrinking
    # the original image
    image = Image.open(thumb_temp)
    for thumb_name, size in THUMB_SIZES_VIDEO:
        image.thumbnail(size, Image.ANTIALIAS)
        image.save(os.path.join(thumbs_dir, thumb_name), quality=90)


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

    if failed_files:
        print('[+] The following files had errors during execution:')
        for file in failed_files:
            print('\t%s' % file)


if __name__ == '__main__':
    main()
