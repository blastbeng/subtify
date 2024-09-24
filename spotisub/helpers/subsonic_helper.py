"""Subsonic helper"""
import logging
import os
import random
import time
import libsonic
from expiringdict import ExpiringDict
from libsonic.errors import DataNotFoundError
from spotisub import spotisub
from spotisub import database
from spotisub import constants
from spotisub import utils
from spotisub.exceptions import SubsonicOfflineException
from spotisub.exceptions import SpotifyApiException
from spotisub.classes import ComparisonHelper
from spotisub.helpers import musicbrainz_helper


if os.environ.get(constants.SPOTDL_ENABLED,
                  constants.SPOTDL_ENABLED_DEFAULT_VALUE) == "1":
    from spotisub.helpers import spotdl_helper
    logging.warning(
        "You have enabled SPOTDL integration, " +
        "make sure to configure the correct download " +
        "path and check that you have enough disk space " +
        "for music downloading.")

if os.environ.get(constants.LIDARR_ENABLED,
                  constants.LIDARR_ENABLED_DEFAULT_VALUE) == "1":
    from spotisub.helpers import lidarr_helper
    logging.warning(
        "You have enabled LIDARR integration, " +
        "if an artist won't be found inside the " +
        "lidarr database, the download process will be skipped.")

pysonic = libsonic.Connection(
    os.environ.get(
        constants.SUBSONIC_API_HOST),
    os.environ.get(
        constants.SUBSONIC_API_USER),
    os.environ.get(
        constants.SUBSONIC_API_PASS),
    appName="Spotisub",
    serverPath=os.environ.get(
        constants.SUBSONIC_API_BASE_URL,
        constants.SUBSONIC_API_BASE_URL_DEFAULT_VALUE) +
    "/rest",
    port=int(
        os.environ.get(
            constants.SUBSONIC_API_PORT)))


#caches
playlist_cache = ExpiringDict(max_len=500, max_age_seconds=300)
spotify_artist_cache = ExpiringDict(max_len=500, max_age_seconds=300)
spotify_song_cache = ExpiringDict(max_len=500, max_age_seconds=300)

def check_pysonic_connection():
    """Return SubsonicOfflineException if pysonic is offline"""
    if pysonic.ping():
        return pysonic
    raise SubsonicOfflineException()


def get_artists_array_names():
    """get artists array names"""
    check_pysonic_connection().getArtists()

    artist_names = []

    for index in check_pysonic_connection().getArtists()["artists"]["index"]:
        for artist in index["artist"]:
            if "name" in artist:
                artist_names.append(artist["name"])

    return artist_names


def search_artist(artist_name):
    """search artist"""

    for index in check_pysonic_connection().getArtists()["artists"]["index"]:
        for artist in index["artist"]:
            if "name" in artist:
                if artist_name.strip().lower(
                ) == artist["name"].strip().lower():
                    return artist["name"]

    return None


def get_subsonic_search_results(text_to_search):
    """get subsonic search results"""
    result = {}
    set_searches = utils.generate_compare_array(text_to_search)
    for set_search in set_searches:
        subsonic_search = check_pysonic_connection().search2(set_search, songCount=500)
        if ("searchResult2" in subsonic_search
            and len(subsonic_search["searchResult2"]) > 0
                and "song" in subsonic_search["searchResult2"]):
            for song in subsonic_search["searchResult2"]["song"]:
                if "id" in song and song["id"] not in result:
                    result[song["id"]] = song
    return result


def get_playlist_id_by_name(playlist_name):
    """get playlist id by name"""
    playlist_id = None
    playlists_search = check_pysonic_connection().getPlaylists()
    if "playlists" in playlists_search and len(
            playlists_search["playlists"]) > 0:
        single_playlist_search = playlists_search["playlists"]
        if "playlist" in single_playlist_search and len(
                single_playlist_search["playlist"]) > 0:
            for playlist in single_playlist_search["playlist"]:
                if playlist["name"].strip() == playlist_name.strip():
                    playlist_id = playlist["id"]
                    break
    return playlist_id


def has_isrc(track):
    """check if spotify track has isrc"""
    if ("external_ids" not in track
        or track["external_ids"] is None
        or "isrc" not in track["external_ids"]
        or track["external_ids"]["isrc"] is None
            or track["external_ids"]["isrc"] == ""):
        return False
    return True


def add_missing_values_to_track(sp, track):
    """calls spotify if tracks has missing album or isrc or uri"""
    if "id" in track:
        uri = 'spotify:track:' + track['id']
        if "album" not in track or has_isrc(track) is False:
            track = sp.track(uri)
            time.sleep(1)
        elif "uri" not in track:
            track["uri"] = uri
        return track
    return None


def write_playlist(sp, playlist_name, results):
    """write playlist to subsonic db"""
    try:
        playlist_name = os.environ.get(
            constants.PLAYLIST_PREFIX,
            constants.PLAYLIST_PREFIX_DEFAULT_VALUE).replace(
            "\"",
            "") + playlist_name
        playlist_id = get_playlist_id_by_name(playlist_name)
        song_ids = []
        old_song_ids = []
        if playlist_id is None:
            check_pysonic_connection().createPlaylist(name=playlist_name, songIds=[])
            logging.info('Creating playlist %s', playlist_name)
            playlist_id = get_playlist_id_by_name(playlist_name)
            database.delete_playlist_relation_by_id(playlist_id)
        else:
            old_song_ids = get_playlist_songs_ids_by_id(playlist_id)

        track_helper = []
        for track in results['tracks']:
            track = add_missing_values_to_track(sp, track)
            found = False
            for artist_spotify in track['artists']:
                if found is False:
                    excluded = False
                    if artist_spotify != '' and "name" in artist_spotify:
                        logging.info(
                            'Searching %s - %s in your music library',
                            artist_spotify["name"],
                            track['name'])
                        if "name" in track:
                            comparison_helper = ComparisonHelper(track,
                                                                 artist_spotify,
                                                                 found,
                                                                 excluded,
                                                                 song_ids,
                                                                 track_helper)
                            comparison_helper = match_with_subsonic_track(
                                comparison_helper,
                                playlist_id,
                                old_song_ids,
                                playlist_name)

                            track = comparison_helper.track
                            artist_spotify = comparison_helper.artist_spotify
                            found = comparison_helper.found
                            excluded = comparison_helper.excluded
                            song_ids = comparison_helper.song_ids
                            track_helper = comparison_helper.track_helper
                    if not excluded:
                        if (os.environ.get(constants.SPOTDL_ENABLED,
                                           constants.SPOTDL_ENABLED_DEFAULT_VALUE) == "1"
                                and found is False):
                            if "external_urls" in track and "spotify" in track["external_urls"]:
                                is_monitored = True
                                if (os.environ.get(constants.LIDARR_ENABLED,
                                                   constants.LIDARR_ENABLED_DEFAULT_VALUE) == "1"):
                                    is_monitored = lidarr_helper.is_artist_monitored(
                                        artist_spotify["name"])
                                if is_monitored:
                                    logging.warning(
                                        'Track %s - %s not found in your music ' +
                                        'library, using SPOTDL downloader',
                                        artist_spotify["name"],
                                        track['name'])
                                    logging.warning(
                                        'This track will be available after ' +
                                        'navidrome rescans your music dir')
                                    spotdl_helper.download_track(
                                        track["external_urls"]["spotify"])
                                else:
                                    logging.warning(
                                        'Track %s - %s not found in your music library',
                                        artist_spotify["name"],
                                        track['name'])
                                    logging.warning(
                                        'This track hasn'
                                        't been found in your Lidarr database, ' +
                                        'skipping download process')
                        elif found is False:
                            logging.warning(
                                'Track %s - %s not found in your music library',
                                artist_spotify["name"],
                                track['name'])
                            database.insert_song(
                                playlist_id, None, artist_spotify, track)
        if playlist_id is not None:

            if len(song_ids) > 0:
                check_pysonic_connection().createPlaylist(
                    playlistId=playlist_id, songIds=song_ids)
                logging.info('Success! Created playlist %s', playlist_name)
            elif len(song_ids) == 0:
                try:
                    check_pysonic_connection().deletePlaylist(playlist_id)
                    logging.info(
                        'Fail! No songs found for playlist %s',
                        playlist_name)
                except DataNotFoundError:
                    pass

    except SubsonicOfflineException:
        logging.error(
            'There was an error creating a Playlist, perhaps is your Subsonic server offline?')


def match_with_subsonic_track(
        comparison_helper, playlist_id, old_song_ids, playlist_name):
    """compare spotify track to subsonic one"""
    text_to_search = comparison_helper.artist_spotify["name"] + \
        " " + comparison_helper.track['name']
    subsonic_search_results = get_subsonic_search_results(text_to_search)
    skipped_songs = []
    for song_id in subsonic_search_results:
        song = subsonic_search_results[song_id]
        song["isrc-list"] = musicbrainz_helper.get_isrc_by_id(song)
        placeholder = song["artist"] + " " + \
            song["title"] + " " + song["album"]
        if song["id"] in old_song_ids:
            logging.info(
                'Track with id "%s" already in playlist "%s"',
                song["id"],
                playlist_name)
            comparison_helper.song_ids.append(song["id"])
            comparison_helper.found = True
            database.insert_song(
                playlist_id, song, comparison_helper.artist_spotify, comparison_helper.track)
        elif (song["id"] not in comparison_helper.song_ids
              and song["artist"] != ''
              and comparison_helper.track['name'] != ''
              and song["album"] != ''
              and song["title"] != ''):
            album_name = ""
            if ("album" in comparison_helper.track
                and "name" in comparison_helper.track["album"]
                    and comparison_helper.track["album"]["name"] is not None):
                album_name = comparison_helper.track["album"]["name"]
            logging.info(
                'Comparing song "%s - %s - %s" with Spotify track "%s - %s - %s"',
                song["artist"],
                song["title"],
                song["album"],
                comparison_helper.artist_spotify["name"],
                comparison_helper.track['name'],
                album_name)
            if has_isrc(comparison_helper.track):
                found_isrc = False
                for isrc in song["isrc-list"]:
                    if isrc.strip(
                    ) == comparison_helper.track["external_ids"]["isrc"].strip():
                        found_isrc = True
                        break
                if found_isrc is True:
                    comparison_helper.song_ids.append(song["id"])
                    comparison_helper.track_helper.append(placeholder)
                    comparison_helper.found = True
                    database.insert_song(
                        playlist_id, song, comparison_helper.artist_spotify, comparison_helper.track)
                    logging.info(
                        'Adding song "%s - %s - %s" to playlist "%s", matched by ISRC: "%s"',
                        song["artist"],
                        song["title"],
                        song["album"],
                        playlist_name,
                        comparison_helper.track["external_ids"]["isrc"])
                    check_pysonic_connection().createPlaylist(
                        playlistId=playlist_id, songIds=comparison_helper.song_ids)
                    break
            if (utils.compare_string_to_exclusion(song["title"],
                utils.get_excluded_words_array())
                or utils.compare_string_to_exclusion(song["album"],
                                                     utils.get_excluded_words_array())):
                comparison_helper.excluded = True
            elif (utils.compare_strings(comparison_helper.artist_spotify["name"], song["artist"])
                  and utils.compare_strings(comparison_helper.track['name'], song["title"])
                  and placeholder not in comparison_helper.track_helper):
                if (("album" in comparison_helper.track and "name" in comparison_helper.track["album"]
                    and utils.compare_strings(comparison_helper.track['album']['name'], song["album"]))
                    or ("album" not in comparison_helper.track)
                        or ("album" in comparison_helper.track and "name" not in comparison_helper.track["album"])):
                    comparison_helper.song_ids.append(song["id"])
                    comparison_helper.track_helper.append(placeholder)
                    comparison_helper.found = True
                    database.insert_song(
                        playlist_id, song, comparison_helper.artist_spotify, comparison_helper.track)
                    logging.info(
                        'Adding song "%s - %s - %s" to playlist "%s", matched by text comparison',
                        song["artist"],
                        song["title"],
                        song["album"],
                        playlist_name)
                    check_pysonic_connection().createPlaylist(
                        playlistId=playlist_id, songIds=comparison_helper.song_ids)
                    break
                skipped_songs.append(song)
    if comparison_helper.found is False and comparison_helper.excluded is False and len(
            skipped_songs) > 0:
        random.shuffle(skipped_songs)
        for skipped_song in skipped_songs:
            placeholder = skipped_song["artist"] + " " + \
                skipped_song['title'] + " " + skipped_song["album"]
            if placeholder not in comparison_helper.track_helper:
                comparison_helper.track_helper.append(placeholder)
                comparison_helper.song_ids.append(skipped_song["id"])
                comparison_helper.found = True
                database.insert_song(
                    playlist_id, skipped_song, comparison_helper.artist_spotify, comparison_helper.track)
                logging.warning(
                    'No matching album found for Subsonic search "%s", using a random one',
                    text_to_search)
                logging.info(
                    'Adding song "%s - %s - %s" to playlist "%s", random match',
                    skipped_song["artist"],
                    song["title"],
                    skipped_song["album"],
                    playlist_name)
                check_pysonic_connection().createPlaylist(
                    playlistId=playlist_id, songIds=comparison_helper.song_ids)
    return comparison_helper

def count_playlists(missing_only=False):
    return database.count_playlists(missing_only)

def select_all_playlists(missing_only=False, page=None, limit=None):
    """get list of playlists and songs"""
    try:
        playlist_songs = database.select_all_playlists(missing_only=missing_only, page=page, limit=limit)

        has_been_deleted = False

        songs = []

        for row in playlist_songs:
            playlist_search, has_been_deleted = get_playlist_from_cache(row.subsonic_playlist_id)

        if has_been_deleted:
            return select_all_playlists(missing_only=missing_only, page=page, limit=limit)

        return playlist_songs, playlist_cache
    except SubsonicOfflineException as ex:
        raise ex

def get_playlist_from_cache(key):
    has_been_deleted = False
    if key not in playlist_cache:
        try:
            playlist_search = check_pysonic_connection().getPlaylist(key)
            playlist_cache[key] = playlist_search["playlist"]["name"]
        except DataNotFoundError:
            pass

    if key not in playlist_cache:
        logging.warning(
            'Playlist id "%s" not found, may be you deleted this playlist from Subsonic?',
            key)
        logging.warning(
            'Deleting Playlist with id "%s" from spotisub database.', key)
        database.delete_playlist_relation_by_id(key)
        has_been_deleted = True

    return playlist_cache[key], has_been_deleted

def get_playlist_songs_ids_by_id(key):
    """get playlist songs ids by id"""
    songs = []
    playlist_search = None
    try:
        playlist_search = check_pysonic_connection().getPlaylist(key)
    except SubsonicOfflineException as ex:
        raise ex
    except DataNotFoundError:
        pass
    if playlist_search is None:
        logging.warning(
            'Playlist id "%s" not found, may be you ' +
            'deleted this playlist from Subsonic?',
            key)
        logging.warning(
            'Deleting Playlist with id "%s" from spotisub database.', key)
        database.delete_playlist_relation_by_id(key)
    elif (playlist_search is not None
            and "playlist" in playlist_search
            and "entry" in playlist_search["playlist"]
            and len(playlist_search["playlist"]["entry"]) > 0):
        songs = playlist_search["playlist"]["entry"]
        for entry in playlist_search["playlist"]["entry"]:
            if "id" in entry and entry["id"] is not None and entry["id"].strip(
            ) != "":
                songs.append(entry["id"])

    return songs


def remove_subsonic_deleted_playlist():
    """fix user manually deleted playlists"""

    spotisub_playlists = database.select_all_playlists(False)
    for key in spotisub_playlists:
        playlist_search = None
        try:
            playlist_search = check_pysonic_connection().getPlaylist(key)
        except SubsonicOfflineException as ex:
            raise ex
        except DataNotFoundError:
            pass
        if playlist_search is None:
            logging.warning(
                'Playlist id "%s" not found, may be you ' +
                'deleted this playlist from Subsonic?',
                key)
            logging.warning(
                'Deleting Playlist with id "%s" from spotisub database.', key)
            database.delete_playlist_relation_by_id(key)

    # DO we really need to remove spotify songs even if they are not related to any playlist?
    # This can cause errors when an import process is running
    # I will just leave spotify songs saved in Spotisub database for now

def load_artist(uuid, spotipy_helper):
    artist_db, songs_db = database.select_artist(uuid)
    songs = []
    sp = None
    for song_db in songs_db:
        spotify_track = None
        if song_db.spotify_song_uuid not in spotify_song_cache:
            sp = sp if sp is not None else spotipy_helper.get_spotipy_client()
            spotify_track = sp.track(song_db.spotify_uri)
            spotify_song_cache[song_db.spotify_song_uuid] = spotify_track
        else:
            spotify_track = spotify_song_cache[song_db.spotify_song_uuid]
        if spotify_track is None:
            raise SpotifyApiException

        
        get_playlist_from_cache(song_db.subsonic_playlist_id)
        song = {}
        song["subsonic_playlist_id"] = song_db.subsonic_playlist_id
        song["subsonic_song_id"] = song_db.subsonic_song_id
        song["title"] = song_db.title
        song["spotify_song_uuid"] = song_db.spotify_song_uuid
        if "album" in spotify_track and "name" in spotify_track["album"]:
            song["spotify_album"] = spotify_track["album"]["name"]
        else:
            song["spotify_album"] = ""

        songs.append(song)
            

    spotify_artist = None

    if uuid not in spotify_artist_cache:
        sp = sp if sp is not None else spotipy_helper.get_spotipy_client()
        spotify_artist = sp.artist(artist_db.spotify_uri)
        spotify_artist_cache[uuid] = spotify_artist
    else:
        spotify_artist = spotify_artist_cache[uuid]

    if spotify_artist is None:
        raise SpotifyApiException
    artist = {}
    artist["name"] = artist_db.name
    artist["genres"] = ""
    if "genres" in spotify_artist:
        artist["genres"] = ", ".join(spotify_artist["genres"])
    artist["image"] = ""
    if "images" in spotify_artist and len(spotify_artist["images"]) > 0:
        artist["image"] = spotify_artist["images"][0]["url"]
    return artist, songs, playlist_cache