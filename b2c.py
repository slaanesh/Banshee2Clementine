#!/usr/bin/env python

import sys
import os
import argparse
import sqlite3
import logging
import urllib
import mimetypes

class B2C:
    """
    Import banshee DB into clementine DB
    """

    def __init__(self):
        """
        Constructor
        """

        parser = argparse.ArgumentParser(description='Import banshee data into clementine DB')
        parser.add_argument('--banshee-db', dest='banshee_db', action='store', required=False,
                            default=os.path.join(os.getenv('HOME'), '.config', 'banshee-1', 'banshee.db'),
                            help='path to banshee DB if not automatically found')
        parser.add_argument('--clementine-db', dest='clementine_db', action='store', required=False,
                            default=os.path.join(os.getenv('HOME'), '.config', 'Clementine', 'clementine.db'),
                            help='path to clementine DB if not automatically found')
        parser.add_argument('--update-stats', dest='update_stats', action='store_true', required=False,
                            default=False,
                            help='update score, play count and skip counts')
        parser.add_argument('--import-playlists', dest='import_playlists', action='store_true', required=False,
                            default=False,
                            help='import banshee playlists')

        self.args = parser.parse_args()

        if not os.path.isfile(self.args.banshee_db):
            raise ValueError('"Cannot find banshee db file at "%s"' % self.args.banshee_db)

        if not os.path.isfile(self.args.clementine_db):
            raise ValueError('"Cannot find clementine db file at "%s"' % self.args.clementine_db)

        self.banshee = sqlite3.connect(self.args.banshee_db, timeout=5.0, detect_types = sqlite3.PARSE_DECLTYPES)
        self.banshee.isolation_level = None
        self.banshee.text_factory = str
        self.banshee.row_factory = sqlite3.Row

        self.clementine = sqlite3.connect(self.args.clementine_db, timeout=5.0, detect_types = sqlite3.PARSE_DECLTYPES)
        self.clementine.isolation_level = None
        self.clementine.text_factory = str
        self.clementine.row_factory = sqlite3.Row

        logging.basicConfig(file=sys.stderr, level=logging.INFO, format='%(asctime)-15s [+] %(levelname)s %(message)s')

    def run(self):
        """
        Run application
        """

        ban_cursor = self.banshee.cursor()

        if self.args.update_stats:
            logging.info('Updating songs statistics from banshee ...')
            query = 'select uri, rating, PlayCount, SkipCount, LastPlayedStamp from CoreTracks;'

            ban_cursor.execute(query)
            nb_items = 0
            for item in ban_cursor:
                if item['uri'] is None:
                    logging.warn('uri is None: %s', item)
                path = self._uri_to_path(item['uri'])
                if os.path.isfile(path) and self._is_audio_file(path):
                    nb_items += 1
                    row_id = self._get_clementine_library_id(path)
                    if row_id is None:
                        logging.warn('%s is missing', path)
                    else:
                        self._update_meta_data(row_id, item['uri'], item['rating'],
                                item['PlayCount'], item['SkipCount'],
                                item['LastPlayedStamp'])
                else:
                    logging.warn('%s is not a file', path)

            logging.info('Checked %d files', nb_items)

        if self.args.import_playlists:
            self._get_clementine_playlists()

            logging.info('Importing playlists ...')
            query = """
            SELECT
                PlaylistID,
                Name,
                COUNT(1) AS nb_items
            FROM
                CorePlaylists
                INNER JOIN CorePlaylistEntries USING (PlaylistID)
            WHERE IsTemporary = 0
            GROUP BY
                PlaylistID,
                Name
            HAVING
                COUNT(1) > 0
            ORDER BY
                nb_items DESC
            ;
            """

            ban_cursor.execute(query)

            query = """
            SELECT
                uri
            FROM
                CorePlaylistEntries
                INNER JOIN CoreTracks USING (TrackID)
            WHERE PlaylistID = :playlist_id
            ORDER BY
                ViewOrder ASC
            ;
            """;
            pl_cursor = self.banshee.cursor()
            for pl in ban_cursor:
                if pl['Name'] not in self.clem_playlists.values():
                    logging.info('Adding playlist "%s" (%d items)', pl['Name'], pl['nb_items'])
                    pl_cursor.execute(query, {'playlist_id': pl['PlaylistID']})
                    self._parse_playlist(pl_cursor, pl['Name'])
                else:
                    logging.warn('Playlist "%s" already there, ignoring it', pl['Name'])


        self.clementine.commit()
        sys.exit(0)

    def _uri_to_path(self, uri):
        """
        Convert an URI into a path
        """
        uri = urllib.unquote(uri)
        if uri.startswith('file:///'):
            uri = uri[7:]

        return uri

    def _is_audio_file(self, path):
        """
        Returns True if the path is an audio file
        """

        return mimetypes.guess_type(path)[0].split('/')[0] == 'audio'

    def _path_not_in_clementine(self, path):
        """
        Checks whether if a path is already path of clementine collection
        """

        cursor = self.clementine.cursor()
        query = 'SELECT 1 FROM songs WHERE filename like :filename;'
        cursor.execute(query, {'filename': self._get_clementine_filename(path)})

        return cursor.fetchone() == None

    def _check_urlencode(self, path):
        """ Checks if a string is urlencoded... not foolproof, but good enough
        """
        if '%20' in path and ' ' not in path:
            return True
        else:
            return False

    def _get_clementine_filename(self, path):
        """ Converts any path to a clemintine path """
        if not self._check_urlencode(path):
            path = urllib.quote(path)
        if not path.startswith('file://'):
            path = 'file://' + path

        # clemintine stores some characters unencoded... it's not consistent with
        # the library.
        path = (path.replace('%2C', ',').replace('%28', '(')
                    .replace('%29', ')').replace('%27', "'")
                    .replace('%26', '&').replace('%2B', '+')
                    .replace('%21', '!').replace('%3B', ';')
                    .replace('%3D', '=').replace('%7E', '~')
                    .replace('%40', '@').replace('%24', '$')
                )

        return path

    def _get_banshee_filename(self, path):
        """ Converts any path to a banshee path """
        if not self._check_urlencode(path):
            path = urllib.quote(path)
        if not path.startswith('file://'):
            path = 'file://' + path

        return path

    def _update_meta_data(self, row_id, path, rating, playcount, skipcount, lastplayed):
        """
        Update clementine DB based of banshee stats if needed
        """

        cursor = self.clementine.cursor()
        query = """
        UPDATE
            songs
        SET
            rating = :rating1,
            playcount = :playcount1,
            skipcount = :skipcount1,
            lastplayed = :lastplayed1
        WHERE rowid = :rowid1
            AND (rating != :rating2 OR playcount != :playcount2
            OR skipcount != :skipcount2 OR lastplayed != :lastplayed2)
        ;
        """
        cursor.execute(query, {
                'rating1': rating,
                'playcount1': playcount,
                'skipcount1': skipcount,
                'lastplayed1': lastplayed,
                'rating2': rating,
                'playcount2': playcount,
                'skipcount2': skipcount,
                'lastplayed2': lastplayed,
                'rowid1': row_id,
                })

        if cursor.rowcount != 0:
            logging.info('%s statistics updated (rating: %d - playcount added: %d - skipcount added: %d)',
                         path, rating, playcount, skipcount)

    def _get_clementine_playlists(self):
        """
        Retrieve existing clementine playlists
        """

        logging.info('Fetching existing playlist from clementine')

        self.clem_playlists = {}
        query = 'SELECT name FROM playlists ORDER BY ui_order;'
        cursor = self.clementine.cursor()
        cursor.execute(query)
        offset = 1
        for item in cursor:
            self.clem_playlists[offset] = item['name']
            offset += 1

    def _get_clementine_library_id(self, path):
        """
        Returns the library ID from a given file path
        """

        cursor = self.clementine.cursor()
        query = 'SELECT rowid FROM songs WHERE filename like :filename;'
        path = self._get_clementine_filename(path)
        cursor.execute(query, {'filename': path})
        row = cursor.fetchone()
        if not row:
            raise ValueError('Cannot find entry "%s" in clementine DB' % path)

        return row['rowid']

    def _parse_playlist(self, pl_cursor, playlist):
        """
        Add playlist to clementine if needed
        """

        playlist_id = len(self.clem_playlists) + 1
        nb_added = 0

        cursor = self.clementine.cursor()
        query = 'INSERT INTO playlist_items(playlist, type, library_id) VALUES(:playlist_id, :type, :library_id);'
        for pl_item in pl_cursor:
            path = self._uri_to_path(pl_item['uri'])
            if os.path.isfile(path) and self._is_audio_file(path):
                library_id = self._get_clementine_library_id(path)
                nb_added += 1
                cursor.execute(query, {
                        'playlist_id': playlist_id,
                        'type': 'Library',
                        'library_id': library_id
                        })

        if nb_added > 0:
            query = 'INSERT INTO playlists(name, ui_order) VALUES(:name, :ui_order);'
            cursor.execute(query, {'name': playlist, 'ui_order': playlist_id})

            self.clem_playlists[playlist_id] = playlist


if __name__ == '__main__':
    b2c = B2C()
    b2c.run()

sys.exit(1)
