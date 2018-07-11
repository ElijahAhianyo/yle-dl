# -*- coding: utf-8 -*-

from __future__ import print_function, absolute_import, unicode_literals
import codecs
import json
import logging
import os.path
import sys
from .utils import sane_filename
from .http import download_to_file
from .backends import IOCapability, Subprocess
from .exitcodes import to_external_rd_code, RD_SUCCESS, RD_INCOMPLETE, \
    RD_FAILED, RD_SUBPROCESS_EXECUTE_FAILED
from .streamfilters import normalize_language_code
from .streams import InvalidStream


logger = logging.getLogger('yledl')


class SubtitleDownloader(object):
    def select_and_download(self, subtitles, videofilename, filters):
        """Filter subtitles and save them to disk.

        Returns a list of filenames where subtitles were saved.
        """
        selected = self.select(subtitles, filters)
        return self.download(selected, videofilename)

    def select(self, subtitles, filters):
        """Return a list of subtitles that match the filters."""
        if filters.hardsubs:
            return []

        selected = []
        for sub in subtitles:
            matching_lang = (filters.sublang_matches(sub.lang, '') or
                             filters.sublang == 'all')
            if sub.url and matching_lang:
                selected.append(sub)

        if selected and filters.sublang != 'all':
            selected = selected[:1]

        return selected

    def download(self, subtitles, videofilename):
        """Download each subtitle and save them to disk.

        Returns a list of filenames where the subtitles were saved.
        """
        basename = os.path.splitext(videofilename)[0]
        subtitlefiles = []
        for sub in subtitles:
            filename = basename + '.' + sub.lang + '.srt'
            if os.path.isfile(filename):
                logger.debug('Subtitle file {} already exists, skipping'
                             .format(filename))
            else:
                try:
                    download_to_file(sub.url, filename)
                    self.add_BOM(filename)
                    logger.info('Subtitles saved to ' + filename)
                    subtitlefiles.append(filename)
                except IOError:
                    logger.exception('Failed to download subtitles '
                                     'at %s' % sub.url)
        return subtitlefiles

    def add_BOM(self, filename):
        """Add byte-order mark into a file.

        Assumes (but does not check!) that the file is UTF-8 encoded.
        """
        enc = sys.getfilesystemencoding()
        encoded_filename = filename.encode(enc, 'replace')

        with open(encoded_filename, 'rb') as infile:
            content = infile.read()
            if content.startswith(codecs.BOM_UTF8):
                return

        with open(encoded_filename, 'wb') as outfile:
            outfile.write(codecs.BOM_UTF8)
            outfile.write(content)


class YleDlDownloader(object):
    def __init__(self, subtitle_downloader=SubtitleDownloader()):
        self.subtitle_downloader = subtitle_downloader

    def download_clips(self, clips, io, filters, postprocess_command):
        def download(clip, stream):
            downloader = stream.create_downloader()
            if not downloader:
                logger.error('Downloading the stream at %s is not yet '
                             'supported.' % clip.webpage)
                logger.error('Try --showurl')
                return RD_FAILED

            downloader.warn_on_unsupported_feature(io)

            outputfile = self.output_name_for_clip(clip, downloader, io)
            subtitlefiles = self.subtitle_downloader.select_and_download(
                clip.subtitles, outputfile, filters)

            self.log_output_file(outputfile)
            dl_result = downloader.save_stream(outputfile, io)

            if dl_result == RD_SUCCESS:
                self.log_output_file(outputfile, True)
                self.postprocess(postprocess_command, outputfile,
                                 subtitlefiles)

            return (dl_result, outputfile)

        def needs_retry(res):
            return res not in [RD_SUCCESS, RD_INCOMPLETE]

        return self.process(clips, download, needs_retry, filters)

    def pipe(self, clips, io, filters):
        def pipe_clip(clip, stream):
            dl = stream.create_downloader()
            if not dl:
                logger.error('Downloading the stream at %s is not yet '
                             'supported.' % clip.webpage)
                return RD_FAILED
            dl.warn_on_unsupported_feature(io)
            subtitles = self.subtitle_downloader.select(clip.subtitles, filters)
            subtitle_url = subtitles[0].url if subtitles else None
            res = dl.pipe(io, subtitle_url)
            return (res, None)

        def needs_retry(res):
            return res == RD_SUBPROCESS_EXECUTE_FAILED

        return self.process(clips, pipe_clip, needs_retry, filters)

    def get_urls(self, clips, filters):
        urls = []
        for clip in clips:
            streams = self.select_streams(clip.flavors, filters)
            if streams and any(s.is_valid() for s in streams):
                valid_stream = next(s for s in streams if s.is_valid())
                urls.append(valid_stream.to_url())
        return urls

    def get_episode_pages(self, clips):
        return [clip.webpage for clip in clips]

    def get_titles(self, clips, io):
        return [sane_filename(m.get('title', ''), io.excludechars)
                for m in self.metadata_generator(clips)]

    def metadata_generator(self, clips):
        return (clip.metadata() for clip in clips)

    def get_metadata(self, clips):
        return [json.dumps(list(self.metadata_generator(clips)), indent=2)]

    def process(self, clips, streamfunc, needs_retry, filters):
        if not clips:
            logger.error('No streams found')
            return RD_SUCCESS

        overall_status = RD_SUCCESS
        for clip in clips:
            streams = self.select_streams(clip.flavors, filters)

            if not streams:
                logger.error('No stream found')
                overall_status = RD_FAILED
            elif all(not stream.is_valid() for stream in streams):
                logger.error('Unsupported stream: %s' %
                             streams[0].get_error_message())
                overall_status = RD_FAILED
            else:
                res = self.try_all_streams(
                    streamfunc, clip, streams, needs_retry)
                if res != RD_SUCCESS and overall_status != RD_FAILED:
                    overall_status = res

        return to_external_rd_code(overall_status)

    def try_all_streams(self, streamfunc, clip, streams, needs_retry):
        latest_result = RD_FAILED
        output_file = None
        for stream in streams:
            if stream.is_valid():
                # Remove if there is a partially downloaded file from the
                # earlier failed stream
                if output_file:
                    self.remove_retry_file(output_file)
                    output_file = None
                
                downloader = stream.create_downloader()
                dlname = downloader and downloader.name
                logger.debug('Now trying downloader {}'.format(dlname))

                (latest_result, output_file) = streamfunc(clip, stream)
                if needs_retry(latest_result):
                    continue

                return latest_result

        return latest_result

    def select_flavor(self, flavors, filters):
        if not flavors:
            return None

        logger.debug('Available flavors: {}'.format([{
            'bitrate': fl.bitrate,
            'height': fl.height,
            'width': fl.width,
            'hard_subtitle': fl.hard_subtitle
        } for fl in flavors]))
        logger.debug('max_height: {}, max_bitrate: {}'.format(
            filters.maxheight, filters.maxbitrate))

        filtered = self.apply_hard_subtitle_filter(flavors, filters)
        filtered = self.apply_resolution_filters(filtered, filters)

        if filtered:
            selected = filtered[-1]
            logger.debug('Selected flavor: {}'.format(selected))
        else:
            selected = None

        return selected

    def apply_hard_subtitle_filter(self, flavors, filters):
        if filters.hardsubs:
            return [
                fl for fl in flavors
                if (fl.hard_subtitle and
                    normalize_language_code(fl.hard_subtitle.lang, None)
                    == filters.hardsubs)
            ]
        else:
            return [fl for fl in flavors if not fl.hard_subtitle]

    def apply_resolution_filters(self, flavors, filters):
        def sort_max_bitrate(x):
            return x.bitrate or 0

        def sort_max_resolution_min_bitrate(x):
            return (x.height or 0, -(x.bitrate or 0))

        def sort_max_resolution_max_bitrate(x):
            return (x.height or 0, x.bitrate or 0)

        filtered = [
            fl for fl in flavors
            if (filters.maxbitrate is None or fl.bitrate <= filters.maxbitrate) and
            (filters.maxheight is None or fl.height <= filters.maxheight)
        ]

        if filtered:
            acceptable_flavors = filtered
            reverse = False
            if filters.maxheight is not None and filters.maxbitrate is not None:
                keyfunc = sort_max_resolution_max_bitrate
            elif filters.maxheight is not None:
                keyfunc = sort_max_resolution_min_bitrate
            else:
                keyfunc = sort_max_bitrate
        else:
            acceptable_flavors = flavors
            reverse = filters.maxheight is not None or filters.maxbitrate is not None
            keyfunc = sort_max_bitrate

        return sorted(acceptable_flavors, key=keyfunc, reverse=reverse)

    def select_streams(self, flavors, filters):
        flavor = self.select_flavor(flavors, filters)
        streams = flavor and flavor.streams
        return self.filter_by_backend(streams or [], filters.enabled_backends)

    def filter_by_backend(self, streams, enabled_backends):
        streams_with_backend_names = []
        for s in streams:
            downloader = s.create_downloader()
            if s.is_valid() and downloader:
                streams_with_backend_names.append((s, downloader.name))

        filtered = []
        for be in enabled_backends:
            for (stream, stream_be) in streams_with_backend_names:
                if stream_be == be:
                    filtered.append(stream)

        if filtered:
            return filtered
        elif any(not s.is_valid() for s in streams):
            return [next(s for s in streams if not s.is_valid())]
        elif streams:
            supported_backends = [x[1] for x in streams_with_backend_names]
            return [InvalidStream('Required backend not enabled. '
                                  'Try: --backend {}'
                                  .format(','.join(supported_backends)))]
        else:
            return []

    def output_name_for_clip(self, clip, downloader, io):
        resume_job = (io.resume and
                      IOCapability.RESUME in downloader.io_capabilities)
        extension = downloader.file_extension
        return clip.output_file_name(extension, io, resume_job)

    def log_output_file(self, outputfile, done=False):
        if outputfile and outputfile != '-':
            if done:
                logger.info('Stream saved to ' + outputfile)
            else:
                logger.info('Output file: ' + outputfile)

    def remove_retry_file(self, filename):
        if filename and os.path.isfile(filename):
            logger.debug('Removing the partially downloaded file')
            try:
                os.remove(filename)
            except OSError:
                logger.warn('Failed to remove a partial output file')

    def postprocess(self, postprocess_command, videofile, subtitlefiles):
        if postprocess_command:
            args = [postprocess_command, videofile]
            args.extend(subtitlefiles)
            return Subprocess().execute(args, None)
