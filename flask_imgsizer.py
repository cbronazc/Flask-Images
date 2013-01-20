from __future__ import division

import math
import os
import logging
from cStringIO import StringIO
import datetime
import hashlib
import sys
import base64
import struct
from urlparse import urlparse
from urllib2 import urlopen
from urllib import urlencode
from subprocess import call

import Image as image

from flask import request, current_app, send_file, abort


log = logging.getLogger(__name__)


def encode_int(value):
    return base64.urlsafe_b64encode(struct.pack('>I', int(value))).rstrip('=').lstrip('A')


class ImgSizer(object):
    
    MODE_FIT = 'fit'
    MODE_CROP = 'crop'
    MODE_PAD = 'pad'
    MODES = (MODE_FIT, MODE_CROP, MODE_PAD)
    
    def __init__(self, app=None):
        if app is not None:
            self.init_app(app)

    def init_app(self, app):
        """
        Initialize a :class:`~flask.Flask` application
        for use with this extension. Useful for the factory pattern but
        not needed if you passed your application to the :class:`ImgSizer`
        constructor.

        """
        if not hasattr(app, 'extensions'):
            app.extensions = {}
        app.extensions['imgsizer'] = self

        app.config.setdefault('IMGSIZER_URL', '/imgsizer')
        app.config.setdefault('IMGSIZER_NAME', 'imgsizer')
        app.config.setdefault('IMGSIZER_PATH', ['assets', 'static'])
        app.config.setdefault('IMGSIZER_CACHE', '/tmp/imgsizer')
        app.config.setdefault('IMGSIZER_MAX_AGE', 3600)

        app.add_url_rule(app.config['IMGSIZER_URL'] + '/<path:path>', app.config['IMGSIZER_NAME'], self.handle_request)

    
    def build_url(self, local_path, **kwargs):

        local_path = local_path.strip('/')

        for key in 'background mode width height quality format padding'.split():
            if key in kwargs:
                kwargs[key[0]] = kwargs.pop(key)
        
        # Remote URLs are encoded into the query.
        parsed = urlparse(local_path)
        if parsed.netloc:
            kwargs['u'] = local_path
            local_path = 'remote'

        # Local ones are not.
        else:
            abs_path = self.find_img(local_path)
            if abs_path:
                kwargs['v'] = encode_int(int(os.path.getmtime(abs_path)))
        
        # TODO: Use itsdangerous with local_path as salt
        return current_app.config['IMGSIZER_URL'] + '/' + local_path + '?' + urlencode(kwargs)
        
    def find_img(self, local_path):
        for path_base in current_app.config['IMGSIZER_PATH']:
            path = os.path.join(current_app.root_path, path_base, local_path)
            if os.path.exists(path):
                return path
    
    def resize(self, img, width=None, height=None, mode=None, background=None):
        
        orig_width, orig_height = img.size

        width = min(width, orig_width) if width else None
        height = min(height, orig_height) if height else None
        
        if not img.mode.lower().startswith('rgb'):
            img = img.convert('RGBA')
        
        if width and height:
    
            fit, crop = sorted([
                (width, orig_height * width // orig_width),
                (orig_width * height // orig_height, height)
            ])
    
            if mode == self.MODE_FIT or mode == self.MODE_PAD:
                img = img.resize(fit, image.ANTIALIAS)
                
                if mode == self.MODE_PAD:
                    pad_color = {'white': (255, 255, 255)}.get(str(background).lower(), 0)
                    back = image.new('RGBA', (width, height), pad_color)
                    back.paste(img, (
                        (width  - fit[0]) // 2,
                        (height - fit[1]) // 2
                    ))
                    img = back
            
            elif mode == self.MODE_CROP:
                dx = (crop[0] - width) // 2
                dy = (crop[1] - height) // 2
                img = img.resize(crop, image.ANTIALIAS).crop(
                    (dx, dy, dx + width, dy + height)
                )
            
            else:
                img = img.resize((width, height), image.ANTIALIAS)
        
        elif width:
            height = orig_height * width // orig_width
            img = img.resize((width, height), image.ANTIALIAS)

        elif height:
            width = orig_width * height // orig_height
            img = img.resize((width, height), image.ANTIALIAS)
        
        return img
    

    def handle_request(self, path):

        query = dict(request.args.iteritems())
        
        # TODO: verify the signature.
        
        remote_url = query.get('u')
        if remote_url:
            # Download the remote file.
            path = os.path.join(
                self.cache_root,
                hashlib.md5(remote_url).hexdigest() + os.path.splitext(remote_url)[1]
            )
            if not os.path.exists(path):
                log.info('downloading %s' % remote_url)
                tmp_path = path + '.tmp-' + str(os.getpid())
                fh = open(tmp_path, 'wb')
                fh.write(urlopen(remote_url).read())
                fh.close()
                call(['mv', tmp_path, path])
        else:
            path = self.find_img(path)
            if not path:
                abort(404) # Not found.

        raw_mtime = os.path.getmtime(path)
        mtime = datetime.datetime.utcfromtimestamp(raw_mtime)
        # log.debug('last_modified: %r' % mtime)
        # log.debug('if_modified_since: %r' % request.if_modified_since)
        if request.if_modified_since and request.if_modified_since >= mtime:
            return abort(304) # Not Modified.
        
        
        mode = query.get('m')
        background = query.get('b')
        width = query.get('w')
        width = int(width) if width else None
        height = query.get('h')
        height = int(height) if height else None
        quality = query.get('q')
        quality = int(quality) if quality else 75
        format = query.get('f', '').lower() or os.path.splitext(path)[1][1:] or 'jpeg'
        format = {'jpg' : 'jpeg'}.get(format, format)
        has_version = 'v' in query
                
        cache_key = hashlib.md5(repr((
            path, mode, width, height, quality, format, background
        ))).hexdigest()

        cache_dir = os.path.join(current_app.config['IMGSIZER_CACHE'], cache_key[:2])
        cache_path = os.path.join(cache_dir, cache_key + '.' + format)

        cache_mtime = os.path.getmtime(cache_path) if os.path.exists(cache_path) else None
        
        if not cache_mtime or cache_mtime < raw_mtime:
            
            log.info('resizing %r for %s' % (path, query))
            
            img = image.open(path)
            img = self.resize(img, width=width, height=height, mode=mode, background=background)
            
            try:
                os.makedirs(cache_dir)
            except OSError:
                pass

            cache_file = open(cache_path, 'wb')
            img.save(cache_file, format, quality=quality)
            cache_file.close()
        
        return send_file(cache_path,
            mimetype='image/%s' % format,
            cache_timeout=31536000 if has_version else self.max_age,
        )


def resized_img_src(path, **kw):
    return current_app.extensions['imgsizer'].build_url(path, **kw)

