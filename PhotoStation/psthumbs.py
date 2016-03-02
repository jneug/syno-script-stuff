#!/usr/bin/env python
# Generates thumbnails for the Snyology Photo Station
# Based on the synothumb script by phillips321 (http://www.phillips321.co.uk)
#    - https://github.com/phillips321/synothumbs
#
# To sync the photos and thumbnails to the nas mount the synology
# photos folder over nfs first. For a nas in the local network at
# 192.168.1.55 and a mount point at ~/shared/photo use
# ```
# sudo mount -o rw,bg,hard,resvport,intr,noac,nfc,tcp 192.168.1.55:/volume1/photo ~/shared/photo
# ```
# nfs shares allow the @eaDir folders to transfered properly.
#
# Then run psthumbs with the '--nfs' option set to the mount point:
# ```
# python3 psthumbs.py --nfs ~/shared/photo .
# ```
# This will generate the thumbnails and use rsync to transfer photos
# and thumbnails to the shared folder.
#
# To unmount the share use:
# ```
# diskutil unmount ~/shared/photo
# ```
#
# Tested on Mac OS X.
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
import tempfile
from termcolor import colored

from PIL import Image, ImageChops, ImageFile
from io import StringIO, BytesIO

VERSION = '0.2.2'
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
# Functions                                                                   #
###############################################################################
failed_files = []  # TODO: make concurrent and not global


def log(msg, file=None, thread='+', verbose=False):
    global cfg

    if file is not None:
        msg = msg % colored(file, 'blue')
    msg = '[%s]' % colored(thread, 'green') + ' ' + msg
    if (not verbose) or cfg.verbose:
        print(msg)


def vlog(msg, file=None, thread='+'):
    log(msg, file=file, thread=thread, verbose=True)


def media_queue_builder(rootdir, media_queue):
    # check availalbe tools and appropriate extensions
    allExtensions = [] + IMAGE_EXTENSIONS
    if is_tool('dcraw'):
        allExtensions += RAW_EXTENSIONS
    else:
        log('[+] dcraw not available in PATH, can\'t convert raw images')
    if is_tool('ffmpeg'):
        allExtensions += VIDEO_EXTENSIONS
    else:
        log('[+] ffmpeg not available in PATH, can\'t generate previews ' +
            'for videos')

    # find files and put into queue
    for path, subFolders, files in os.walk(rootdir):
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in allExtensions:  # is a viable file type?
                if THUMB_DIR not in path:
                    if file not in IGNORED_FILES:  # maybe remove (?)
                        media_queue.put(os.path.join(path, file))
                        vlog("[+] Added %s to queue",
                             file=os.path.join(path, file))


def is_tool(name):
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


def get_thumbs_dir(file):
    file_dir, file_name = os.path.split(file)
    return os.path.join(file_dir, THUMB_DIR, file_name)


def make_thumbs_dir(file):
    """
    Check if the thumbnail directory for the given media item exists
    and create it otherwise. Throws an exception if the directory could not
    be created.
    """
    thumbs_dir = get_thumbs_dir(file)
    if not os.path.isdir(thumbs_dir):
        vlog("[-] Creating thumbnail directory at %s", file=thumbs_dir)
        os.makedirs(thumbs_dir, exist_ok=True)


def run_rsync(cfg):
    log('[+] syncing files to %s', file=cfg.nfs_share)

    paths = (os.path.join(cfg.rootdir, ''), cfg.nfs_share)
    rsync_cmd = 'rsync -au --iconv=UTF8-MAC,UTF-8 ' \
                '--prune-empty-dirs "%s" "%s"' % paths
    if cfg.verbose:
        rsync_cmd += ' --progress'
    rsync_proc = subprocess.Popen(shlex.split(rsync_cmd))
    rsync_proc.communicate()


###############################################################################
# Media converter class                                                       #
###############################################################################

class MediaConverter(threading.Thread):

    def __init__(self, cfg, media_queue):
        threading.Thread.__init__(self)
        self.cfg = cfg
        self.media_queue = media_queue

    def run(self):
        while True:
            self.media_item = self.media_queue.get()
            if self.media_item is None:
                break
            self.media_converter()
            self.media_queue.task_done()

    def report(self, msg):
        vlog("[%s] %s" % (colored(self.name, 'green'), msg))

    def media_converter(self):
        self.report('Working on %s' % colored(self.media_item, 'blue'))

        self.thumbs_dir = get_thumbs_dir(self.media_item)
        if os.path.isdir(self.thumbs_dir):
            if not self.cfg.force:
                self.report('%s already processed' %
                            colored(self.media_item, 'blue'))
                return
        else:
            try:
                make_thumbs_dir(self.media_item)
            except Exception as e:
                # Failed to generate thumbs dir (other exceptions possible?)
                failed_files.append(self.media_item)
                self.report("Failed to create thumbnail directory for %s" %
                            colored(self.media_item, 'blue'))
                self.report("Exception: %s" % e)
                return

        ext = os.path.splitext(self.media_item)[1].lower()
        if ext in IMAGE_EXTENSIONS:
            self.image_converter()
        elif ext in RAW_EXTENSIONS:
            self.raw_converter()
        elif ext in VIDEO_EXTENSIONS:
            self.video_converter()

    def image_converter(self):
        """
        Generats thumbnails for image files (with an extension in
        IMAGE_EXTENSIONS).
        """
        self.image = Image.open(self.media_item)
        try:
            self.image.load()
            self.generate_thumbnails()
        except OSError as e:
            try:
                ImageFile.LOAD_TRUNCATED_IMAGES = True
                self.image.load()
                self.generate_thumbnails()
                ImageFile.LOAD_TRUNCATED_IMAGES = False
            except OSError as e2:
                failed_files.append(self.media_item)
                self.report("Failed to read image %s" % self.media_item)
                self.report("Exception: %s" % e2)

    def raw_converter(self):
        """
        Converts raw images (with an extension in RAW_EXTENSIONS) with dcraw
        and generates thumbnails. The command used for all raw files is
        `dcraw -c -b 8 -q 0 -w -H 5 '%s'`.
        """
        self.report("Converting raw image %s" % self.media_item)
        try:
            dcraw_cmd = "dcraw -c -b 8 -q 0 -w -H 5 '%s'" % self.media_item
            dcraw_proc = subprocess.Popen(shlex.split(dcraw_cmd),
                                          stdout=subprocess.PIPE)
            image_raw = BytesIO(dcraw_proc.communicate()[0])
            self.image = Image.open(image_raw)
            self.generate_thumbnails()
        except Exception as e:
            failed_files.append(self.media_item)
            self.report("Failed to convert raw image %s" % self.media_item)
            self.report("Exception: %s" % e)

    def generate_thumbnails(self):
        """
        Generates thumbnail and preview files for the provided image.
        """
        self.rotate_image()

        # Generate thumbnails in all sizes by consecutively shrinking
        # the original image
        for thumb_name, size in THUMB_SIZES:
            self.image.thumbnail(size, Image.ANTIALIAS)
            self.image.save(os.path.join(self.thumbs_dir, thumb_name),
                            quality=90)

        # Generate the preview image
        preview_name, preview_size = PREVIEW_SIZE
        self.image.thumbnail(preview_size, Image.ANTIALIAS)
        # pad out image and save preview image
        image_size = self.image.size
        preview_img = self.image.crop((0, 0, preview_size[0], preview_size[1]))
        offset_x = int(max((preview_size[0] - image_size[0]) / 2, 0))
        offset_y = int(max((preview_size[1] - image_size[1]) / 2, 0))
        preview_img = ImageChops.offset(preview_img, offset_x, offset_y)
        preview_img.save(os.path.join(self.thumbs_dir, preview_name),
                         quality=90)

    def rotate_image(self):
        """
        Attempts to rotate the provided image according to the EXIF information
        found in the image.
        """
        try:
            # code adapted from:
            # http://www.lifl.fr/~riquetd/auto-rotating-pictures-using-pil.html
            exif = self.image._getexif()
            if exif:
                orientation_key = 274  # cf ExifTags
                if orientation_key in exif:
                    orientation = exif[orientation_key]

                    rotate_values = {3: 180, 6: 270, 8: 90}

                    if orientation in rotate_values:
                        self.image = self.image.rotate(
                                        rotate_values[orientation])
        except:
            pass  # could not rotate image, proceed with image as is

    def video_converter(self):
        """
        Generats video previews for video files (with an extension in
        VIDEO_EXTENSIONS).
        """
        # Generate .flv preview video
        ffmpeg_cmd = 'ffmpeg -loglevel panic -i "%s" -y -ar 44100 -r 12 ' \
                     '-ac 2 -f flv -qscale 5 -s 320x180 -aspect 320:180 ' \
                     '-t %i "%s/%s"' % (self.media_item,
                                        self.cfg.video_duration,
                                        self.thumbs_dir,
                                        PREVIEW_SIZE_VIDEO[0])
        # TODO: replace -s and -aspect with PREVIEW_SIZE_VIDEO[1]

        ffmpeg_proc = subprocess.Popen(shlex.split(ffmpeg_cmd),
                                       stdout=subprocess.PIPE)
        ffmpeg_proc.communicate()[0]

        # Generate temporary preview image
        file_dir, file_name = os.path.split(self.media_item)
        thumb_temp = os.path.join(tempfile.gettempdir(),
                                  os.path.splitext(file_name)[0] + ".jpg")
        ffmpeg_thumb_cmd = 'ffmpeg -loglevel panic -i "%s" -y -an -ss %s ' \
            '-an -r 1 -vframes 1 "%s"' % (self.media_item,
                                          self.cfg.video_timecode,
                                          thumb_temp)

        ffmpeg_thumb_proc = subprocess.Popen(shlex.split(ffmpeg_thumb_cmd),
                                             stdout=subprocess.PIPE)
        ffmpeg_thumb_proc.communicate()[0]

        # Generate thumbnails in all sizes by consecutively shrinking
        # the original image
        self.image = Image.open(thumb_temp)
        for thumb_name, size in THUMB_SIZES_VIDEO:
            self.image.thumbnail(size, Image.ANTIALIAS)
            self.image.save(os.path.join(self.thumbs_dir, thumb_name),
                            quality=90)

        # Delete the temporary file
        os.remove(thumb_temp)


###############################################################################
# Main                                                                        #
###############################################################################
if __name__ == '__main__':
    cfg = parser.parse_args()
    media_queue = Queue()  # Initialize media queue

    # Start worker threads
    log("[+] Starting worker threads..")
    threads = []
    for i in range(cfg.num_of_threads):
        t = MediaConverter(cfg, media_queue)
        t.setDaemon(True)
        t.start()
        threads.append(t)

    # Populate media queue and wait for conversions to finish
    log("[+] Looking for media and populating queue..")
    media_queue_builder(cfg.rootdir, media_queue)
    media_queue.join()
    log("[+] All media processed..")

    # Stop workers threads
    log("[+] Terminating worker threads..")
    for i in range(cfg.num_of_threads):
        media_queue.put(None)
    for t in threads:
        t.join()

    log("[+] Thumbnail generation completed in %i seconds" %
        (time.time() - START_TIME))

    if failed_files:
        log('[+] The following files had errors during execution:')
        for file in failed_files:
            log('\t%s' % file)

    if cfg.nfs_share and is_tool('rsync'):
        run_rsync(cfg)
