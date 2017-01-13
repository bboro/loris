# -*- coding: utf-8 -*-
"""
`resolver` -- Resolve Identifiers to Image Paths
================================================
"""
from logging import getLogger
from loris_exception import ResolverException
from os.path import join, exists
from os import makedirs
from os.path import dirname
from shutil import copy
from urllib import unquote, quote_plus
from contextlib import closing
from collections import defaultdict

import constants
import hashlib
import glob
import requests
import re

logger = getLogger(__name__)


class _AbstractResolver(object):
    def __init__(self, config):
        self.config = config

    def is_resolvable(self, ident):
        """
        The idea here is that in some scenarios it may be cheaper to check
        that an id is resolvable than to actually resolve it. For example, for
        an HTTP resolver, this could be a HEAD instead of a GET.

        Args:
            ident (str):
                The identifier for the image.
        Returns:
            bool
        """
        cn = self.__class__.__name__
        raise NotImplementedError('is_resolvable() not implemented for %s' % (cn,))

    def resolve(self, ident):
        """
        Given the identifier of an image, get the path (fp) and format (one of.
        'jpg', 'tif', or 'jp2'). This will likely need to be reimplemented for
        environments and can be as smart or dumb as you want.

        Args:
            ident (str):
                The identifier for the image.
        Returns:
            (str, str): (fp, format)
        Raises:
            ResolverException when something goes wrong...
        """
        cn = self.__class__.__name__
        raise NotImplementedError('resolve() not implemented for %s' % (cn,))


class SimpleFSResolver(_AbstractResolver):
    """
    For this dumb version a constant path is prepended to the identfier
    supplied to get the path It assumes this 'identifier' ends with a file
    extension from which the format is then derived.
    """

    def __init__(self, config):
        super(SimpleFSResolver, self).__init__(config)
        if 'src_img_roots' in self.config:
            self.source_roots = self.config['src_img_roots']
        else:
            self.source_roots = [self.config['src_img_root']]

    def raise_404_for_ident(self, ident):
        message = 'Source image not found for identifier: %s.' % (ident,)
        logger.warn(message)
        raise ResolverException(404, message)

    def source_file_path(self, ident):
        ident = unquote(ident)
        for directory in self.source_roots:
            fp = join(directory, ident)
            if exists(fp):
                return fp

    def is_resolvable(self, ident):
        return not self.source_file_path(ident) is None

    def format_from_ident(self, ident):
        return ident.split('.')[-1].lower()

    def resolve(self, ident):

        if not self.is_resolvable(ident):
            self.raise_404_for_ident(ident)

        source_fp = self.source_file_path(ident)
        logger.debug('src image: %s' % (source_fp,))

        format = self.format_from_ident(ident)
        logger.debug('src format %s' % (format,))

        return (source_fp, format)


# To use this the resolver stanza of the config will have to have both the
# src_img_root as required by the SimpleFSResolver and also an
# [[extension_map]], which will be a hash mapping found extensions to the
# extensions that loris wants to see, e.g.
#
# [resolver]
# impl = 'loris.resolver.ExtensionNormalizingFSResolver'
# src_img_root = '/cnfs-ro/iiif/production/medusa-root' # r--
#   [[extension_map]]
#   jpeg = 'jpg'
#   tiff = 'tif'
# Note that case normalization happens before looking up in the extension_map.
class ExtensionNormalizingFSResolver(SimpleFSResolver):
    def __init__(self, config):
        super(ExtensionNormalizingFSResolver, self).__init__(config)
        self.extension_map = self.config['extension_map']

    def format_from_ident(self, ident):
        format = super(ExtensionNormalizingFSResolver, self).format_from_ident(ident)
        format = format.lower()
        format = self.extension_map.get(format, format)
        return format


class SimpleHTTPResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    an http image store (like Fedora Commons). The first call to `resolve()`
    copies the source image into a local cache; subsequent calls use local
    copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The config dictionary MAY contain
     * `source_prefix`, the url up to the identifier.
     * `source_suffix`, the url after the identifier (if applicable).
     * `default_format`, the format of images (will use content-type of response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests to verify object existence (don't set if using
        Fedora Commons prior to 3.8).
     * `uri_resolvable` with value True, allows one to use full uri's to resolve to an image.
     * `user`, the username to make the HTTP request as.
     * `pw`, the password to make the HTTP request as.
     * `ssl_check`, whether to check the validity of the origin server's HTTPS
     certificate. Set to False if you are using an origin server with a
     self-signed certificate.
     * `cert`, path to an SSL client certificate to use for authentication. If `cert` and `key` are both present, they take precedence over `user` and `pw` for authetication.
     * `key`, path to an SSL client key to use for authentication.
    '''
    def __init__(self, config):
        super(SimpleHTTPResolver, self).__init__(config)

        self.source_prefix = self.config.get('source_prefix', '')

        self.source_suffix = self.config.get('source_suffix', '')

        self.default_format = self.config.get('default_format', None)

        self.head_resolvable = self.config.get('head_resolvable', False)

        self.uri_resolvable = self.config.get('uri_resolvable', False)

        self.user = self.config.get('user', None)

        self.pw = self.config.get('pw', None)

        self.cert = self.config.get('cert', None)

        self.key = self.config.get('key', None)

        self.ssl_check = self.config.get('ssl_check', True)

        self.ident_regex = self.config.get('ident_regex', False)

        if 'cache_root' in self.config:
            self.cache_root = self.config['cache_root']
        else:
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Missing setting for cache_root.'
            logger.error(message)
            raise ResolverException(500, message)

        if not self.uri_resolvable and self.source_prefix == '':
            message = 'Server Side Error: Configuration incomplete and cannot resolve. Must either set uri_resolvable' \
                      ' or source_prefix settings.'
            logger.error(message)
            raise ResolverException(500, message)

    def request_options(self):
        # parameters to pass to all head and get requests;
        options = {}
        if self.cert is not None and self.key is not None:
            options['cert'] = (self.cert, self.key)
        if self.user is not None and self.pw is not None:
            options['auth'] = (self.user, self.pw)
        options['verify'] = self.ssl_check
        return options

    def is_resolvable(self, ident):
        ident = unquote(ident)

        if self.ident_regex:
            regex = re.compile(self.ident_regex)
            if not regex.match(ident):
                return False

        fp = join(self.cache_root, SimpleHTTPResolver._cache_subroot(ident))
        if exists(fp):
            return True
        else:
            (url, options) = self._web_request_url(ident)

            if self.head_resolvable:
                try:
                    with closing(requests.head(url, **options)) as response:
                        if response.ok:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

            else:
                try:
                    with closing(requests.get(url, stream=True, **options)) as response:
                        if response.ok:
                            return True
                except requests.exceptions.MissingSchema:
                    return False

        return False

    def format_from_ident(self, ident, potential_format):
        if self.default_format is not None:
            return self.default_format
        elif potential_format is not None:
            return potential_format
        elif ident.rfind('.') != -1 and (len(ident) - ident.rfind('.') <= 5):
            return ident.split('.')[-1]
        else:
            message = 'Format could not be determined for: %s.' % (ident)
            logger.warn(message)
            raise ResolverException(404, message)

    def _web_request_url(self, ident):
        if (ident[:7] == 'http://' or ident[:8] == 'https://') and self.uri_resolvable:
            url = ident
        else:
            url = self.source_prefix + ident + self.source_suffix
        return (url, self.request_options())

    # Get a subdirectory structure for the cache_subroot through hashing.
    @staticmethod
    def _cache_subroot(ident):
        cache_subroot = ''

        # Split out potential pidspaces... Fedora Commons most likely use case.
        if ident[0:6] != 'http:/' and ident[0:7] != 'https:/' and len(ident.split(':')) > 1:
            for split_ident in ident.split(':')[0:-1]:
                cache_subroot = join(cache_subroot, split_ident)
        elif ident[0:6] == 'http:/' or ident[0:7] == 'https:/':
            cache_subroot = 'http'

        cache_subroot = join(cache_subroot, SimpleHTTPResolver._ident_file_structure(ident))

        return cache_subroot

    # Get the directory structure of the identifier itself
    @staticmethod
    def _ident_file_structure(ident):
        file_structure = ''
        ident_hash = hashlib.md5(quote_plus(ident)).hexdigest()
        # First level 2 digit directory then do three digits...
        file_structure_list = [ident_hash[0:2]] + [ident_hash[i:i+3] for i in range(2, len(ident_hash), 3)]

        for piece in file_structure_list:
            file_structure = join(file_structure, piece)

        return file_structure

    def cache_dir_path(self, ident):
        ident = unquote(ident)
        return join(
                self.cache_root,
                SimpleHTTPResolver._cache_subroot(ident)
        )

    def cache_file_path(self, ident):
        pass

    def raise_404_for_ident(self, ident):
        message = 'Image not found for identifier: %s.' % (ident)
        raise ResolverException(404, message)

    def cached_files_for_ident(self, ident):
        cache_dir = self.cache_dir_path(ident)
        if exists(cache_dir):
            return glob.glob(join(cache_dir, 'loris_cache.*'))
        return []

    def in_cache(self, ident):
        return exists(self.cache_dir_path(ident))

    def cached_object(self, ident):
        cached_files = self.cached_files_for_ident(ident)
        if cached_files:
            cached_object = cached_files[0]
        else:
            self.raise_404_for_ident(ident)
        return cached_object

    def cache_file_extension(self, ident, response):
        if 'content-type' in response.headers:
            try:
                extension = self.format_from_ident(ident, constants.FORMATS_BY_MEDIA_TYPE[response.headers['content-type']])
            except KeyError:
                logger.warn('Your server may be responding with incorrect content-types. Reported %s for ident %s.'
                            % (response.headers['content-type'], ident))
                # Attempt without the content-type
                extension = self.format_from_ident(ident, None)
        else:
            extension = self.format_from_ident(ident, None)
        return extension

    def copy_to_cache(self, ident):
        ident = unquote(ident)
        (source_url, options) = self._web_request_url(ident)

        logger.debug('src image: %s' % (source_url,))

        try:
            response = requests.get(
                    source_url,
                    stream=False,
                    **options
            )
        except requests.exceptions.MissingSchema:
            logger.warn(
                'Bad URL request at %s for identifier: %s.' % (source_url, ident)
            )
            public_message = 'Bad URL request made for identifier: %s.' % (ident,)
            raise ResolverException(404, public_message)

        if not response.ok:
            public_message = 'Source image not found for identifier: %s. Status code returned: %s' % (ident,response.status_code)
            log_message = 'Source image not found at %s for identifier: %s. Status code returned: %s' % (source_url,ident,response.status_code)
            logger.warn(log_message)
            raise ResolverException(404, public_message)

        extension = self.cache_file_extension(ident, response)
        logger.debug('src extension %s' % (extension,))

        cache_dir = self.cache_dir_path(ident)
        local_fp = join(cache_dir, "loris_cache." + extension)

        try:
            makedirs(dirname(local_fp))
        except:
            logger.debug("Directory already existed... possible problem if not a different format")

        with open(local_fp, 'wb') as fd:
            for chunk in response.iter_content(2048):
                fd.write(chunk)

        logger.info("Copied %s to %s" % (source_url, local_fp))

    def resolve(self, ident):
        if not self.in_cache(ident):
            self.copy_to_cache(ident)
        cached_file_path = self.cached_object(ident)
        format = self.format_from_ident(cached_file_path, None)
        logger.debug('src image from local disk: %s' % (cached_file_path,))
        return (cached_file_path, format)


class TemplateHTTPResolver(SimpleHTTPResolver):
    '''HTTP resolver that suppors multiple configurable patterns for supported
    urls.  Based on SimpleHTTPResolver.  Identifiers in URLs should be
    specified as `template_name:id`.

    The configuration MUST contain
     * `cache_root`, which is the absolute path to the directory where source images
        should be cached.

    The configuration SHOULD contain
     * `templates`, a comma-separated list of template names e.g.
        templates=`site1,site2`
     * A subsection named for each template, e.g. `[[site1]]`. This subsection
       MUST contain a `url`, which is a url pattern for each specified template, e.g.
       url='http://example.edu/images/%s' or
       url='http://example.edu/images/%s/master'. It MAY also contain other keys
       from the SimpleHTTPResolver configuration to provide a per-template
       override of these options. Overridable keys are `user`, `pw`,
       `ssl_check`, `cert`, and `key`.

    Note that if a template is listed but has no pattern configured, the
    resolver will warn but not fail.

    The configuration may also include the following settings, as used by
    SimpleHTTPResolver:
     * `default_format`, the format of images (will use content-type of
        response if not specified).
     * `head_resolvable` with value True, whether to make HEAD requests
        to verify object existence (don't set if using Fedora Commons
        prior to 3.8).  [Currently must be the same for all templates]
    '''
    def __init__(self, config):
        # required for simplehttpresolver
        # all templates are assumed to be uri resolvable
        config['uri_resolvable'] = True
        super(TemplateHTTPResolver, self).__init__(config)
        templates = self.config.get('templates', '')
        # technically it's not an error to have no templates configured,
        # but nothing will resolve; is that useful? or should this
        # cause an exception?
        if not templates:
            logger.warn('No templates specified in configuration')
        self.templates = {}
        for name in templates.split(','):
            name = name.strip()
            cfg = self.config.get(name, None)
            if cfg is None:
                logger.warn('No configuration specified for resolver template %s' % name)
            else:
                self.templates[name] = cfg
        logger.debug('TemplateHTTPResolver templates: %s' % str(self.templates))

    def _web_request_url(self, ident):
        # only split identifiers that look like template ids;
        # ignore other requests (e.g. favicon)
        if ':' not in ident:
            return (None, {})
        prefix, ident = ident.split(':', 1)

        url = None
        if 'delimiter' in self.config:
            # uses delimiter of choice from config file to split identifier
            # into tuple that will be fed to template
            ident_components = ident.split(self.config['delimiter'])
            if prefix in self.templates:
                url = self.templates[prefix]['url'] % tuple(ident_components)
        else:
            if prefix in self.templates:
                url = self.templates[prefix]['url'] % ident
        if url is None:
            # if prefix is not recognized, no identifier is returned
            # and loris will return a 404
            return (None, {})
        else:
            # first get the generic options
            options = self.request_options()
            # then add any template-specific ones
            conf = self.templates[prefix]
            if 'cert' in conf and 'key' in conf:
                options['cert'] = (conf['cert'], conf['key'])
            if 'user' in conf and 'pw' in conf:
                options['auth'] = (conf['user'], conf['pw'])
            if 'ssl_check' in conf:
                options['verify'] = conf['ssl_check']
            return (url, options)


class SourceImageCachingResolver(_AbstractResolver):
    '''
    Example resolver that one might use if image files were coming from
    mounted network storage. The first call to `resolve()` copies the source
    image into a local cache; subsequent calls use local copy from the cache.

    The config dictionary MUST contain
     * `cache_root`, which is the absolute path to the directory where images
        should be cached.
     * `source_root`, the root directory for source images.
    '''
    def __init__(self, config):
        super(SourceImageCachingResolver, self).__init__(config)
        self.cache_root = self.config['cache_root']
        self.source_root = self.config['source_root']

    def is_resolvable(self, ident):
        source_fp = self.source_file_path(ident)
        return exists(source_fp)

    def format_from_ident(self, ident):
        return ident.split('.')[-1]

    def source_file_path(self, ident):
        ident = unquote(ident)
        return join(self.source_root, ident)

    def cache_file_path(self, ident):
        ident = unquote(ident)
        return join(self.cache_root, ident)

    def in_cache(self, ident):
        return exists(self.cache_file_path(ident))

    def copy_to_cache(self, ident):
        source_fp = self.source_file_path(ident)
        cache_fp = self.cache_file_path(ident)

        makedirs(dirname(cache_fp))
        copy(source_fp, cache_fp)
        logger.info("Copied %s to %s" % (source_fp, cache_fp))

    def raise_404_for_ident(self, ident):
        source_fp = self.source_file_path(ident)
        public_message = 'Source image not found for identifier: %s.' % (ident,)
        log_message = 'Source image not found at %s for identifier: %s.' % (source_fp,ident)
        logger.warn(log_message)
        raise ResolverException(404, public_message)

    def resolve(self, ident):
        if not self.is_resolvable(ident):
            self.raise_404_for_ident(ident)
        if not self.in_cache(ident):
            self.copy_to_cache(ident)

        cache_fp = self.cache_file_path(ident)
        logger.debug('Image Served from local cache: %s' % (cache_fp,))

        format = self.format_from_ident(ident)
        logger.debug('Source format %s' % (format,))
        return (cache_fp, format)

"""
Resolves IWM's collection object and media ID's.

Identifier format:
object-12345/media-12345/large
object-98765432/media-987654/mid
"""
class IwmFSResolver(SimpleFSResolver):
    def __init__(self, config):
        super(SimpleFSResolver, self).__init__(config)
        if 'src_img_roots' in self.config:
            self.source_roots = self.config['src_img_roots']
        else:
            self.source_roots = [self.config['src_img_root']]

    """
    Raises 404 exception if the identifier is
    not resolvable

    @param self IwmFSResolver
    @param ident string
    """
    def raise_404_for_ident(self, ident):
        message = 'Source image not found for identifier: %s.' % (ident,)
        logger.warn(message)
        raise ResolverException(404, message)

    """
    Parses the json value and returns the path
    to the media file

    @param self IwmFSResolver
    @param json_val list
    @param image_size_id string
    @param media_id string

    @return string | None
    """
    def parse_json_val(self, json_val, image_size_id, media_id):
        num_found = json_val['response']['numFound']

        if num_found > 0:
            docs = json_val['response']['docs']
            media_location = defaultdict(list)

            for el in docs:
                media_reference = el['mediaReference']
                media_locations = el[image_size_id]

                for counter, reference in enumerate(media_reference):
                    if reference == media_id:
                        return media_locations[counter]
        else:
            return None

    """
    Checks whether an identifier is resolvable or not.

    @param self IwmFSResolver
    @param ident string

    @return bool
    """
    def is_resolvable(self, ident):
        return not self.source_file_path(ident) is None

    """
    Returns the file extension using string
    manipulation

    @param self IwmFSResolver
    @param source_fp string

    @return string
    """
    def format_from_source_fp(self, source_fp):
        return source_fp.split('.')[-1]

    """
    Queries the SOLR server and returns
    the file path for a given identifier

    @param self IwmFSResolver
    @param ident string

    @return string
    """
    def source_file_path(self, ident):
        # URL decode the identifier
        ident = unquote(ident) # = object-123456/media-654321/large

        # Split the ident string by forward slashes
        # This should have 3 values
        object_id, media_id, size = ident.split('/')

        image_size_id = size+'MediaLocation'
        request_url = 'http://192.168.100.112:28080/solr-4.10.0/iwm-new/select/'
        data = {'q':'identifier:'+object_id+' AND mediaReference:'+media_id, 'fl':image_size_id+',mediaReference', 'wt':'json'}

        r = requests.get(request_url, params=data)

        fpath = self.parse_json_val(r.json(), image_size_id, media_id)

        if not fpath is None:
            for directory in self.source_roots:
                fp = join(directory, fpath)
                if exists(fp):
                    return fp

    """
    Main method of this class which gets
    called from outside

    @param self IwmFSResolver
    @param ident string

    @return tuple
    """
    def resolve(self, ident):
        # ident = object-123456/media-654321/large
        if not self.is_resolvable(ident):
            self.raise_404_for_ident(ident)

        source_fp = self.source_file_path(ident)
        format = self.format_from_source_fp(source_fp)

        return (source_fp, format)