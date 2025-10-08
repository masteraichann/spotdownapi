from flask import Flask, request, jsonify, send_file
from flask_cors import CORS
import os
import re
import tempfile
import shutil
from pathlib import Path
import traceback
from spotdl import Spotdl
from spotdl.types.options import DownloaderOptions
import logging
from datetime import datetime
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from spotifyscraper import SpotifyClient
import asyncio
import httpx

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Configure CORS for Next.js frontend
CORS(app, resources={
    r"/api/*": {
        "origins": [
            "http://localhost:3000",
            "http://localhost:5000",
            "https://spotdown.cc",
            "https://www.spotdown.cc"
        ],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "expose_headers": ["Content-Disposition"],
        "supports_credentials": True
    }
})

# Spotify API credentials
SPOTIFY_CLIENT_ID = "70cf61a23796474f891f2e0bcd4b6841"
SPOTIFY_CLIENT_SECRET = "942ed41a67834e5c92c7c15f9a93a60a"

# Create temp directory for downloads
TEMP_DIR = tempfile.mkdtemp(prefix="spotdown_")
logger.info(f"Temporary directory created at: {TEMP_DIR}")

# Initialize Spotipy for playlist/album metadata with retry settings
try:
    spotify_client = spotipy.Spotify(
        auth_manager=SpotifyClientCredentials(
            client_id=SPOTIFY_CLIENT_ID,
            client_secret=SPOTIFY_CLIENT_SECRET
        ),
        retries=1,  # Reduce retries to fail fast and use SpotDL fallback
        status_retries=1,
        backoff_factor=0.1  # Reduce backoff time
    )
    logger.info("Spotipy client initialized successfully with retry settings")
except Exception as e:
    logger.error(f"Failed to initialize Spotipy: {str(e)}")
    spotify_client = None

# Initialize Spotify Scraper as additional fallback for editorial/restricted playlists
try:
    scraper_client = SpotifyClient()
    logger.info("Spotify Scraper client initialized successfully")
except Exception as e:
    logger.error(f"Failed to initialize Spotify Scraper: {str(e)}")
    scraper_client = None

async def fetch_track_image(client, track_uri):
    """Fetch album image for a track using Spotify oEmbed API"""
    if not track_uri or not track_uri.startswith('spotify:track:'):
        return None
    
    track_id = track_uri.split(':')[-1]
    track_url = f"https://open.spotify.com/track/{track_id}"
    oembed_url = f"https://open.spotify.com/oembed?url={track_url}"
    
    try:
        response = await client.get(oembed_url, timeout=2.0)
        if response.status_code == 200:
            data = response.json()
            return data.get('thumbnail_url')
    except:
        pass
    return None

async def fetch_all_track_images(track_uris):
    """Fetch all track images concurrently"""
    if not track_uris:
        return {}
    
    async with httpx.AsyncClient() as client:
        tasks = [fetch_track_image(client, uri) for uri in track_uris]
        images = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Filter out exceptions and create mapping
        result = {}
        for uri, img in zip(track_uris, images):
            if not isinstance(img, Exception) and img:
                result[uri] = img
        
        return result

# Initialize SpotDL with credentials and optimized settings
try:
    spotdl = Spotdl(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        downloader_settings={
            "output": TEMP_DIR,
            "format": "mp3",
            "bitrate": "320k",
            "threads": 8,  # Increased threads for faster processing
            "ffmpeg": "ffmpeg",
            "restrict": False,
            "print_errors": False,
            "sponsor_block": False,  # Skip sponsor block checks
            "archive": None,  # Disable archive file
            "concurrent_downloads": 1,
            "embed_metadata": True,
            "generate_lrc": False,  # Disable lyrics generation
            "save_file": None,  # No save file
            "preload": False,  # Disable preload
            "audio_providers": ["youtube-music", "youtube"],  # Use YouTube Music as primary, YouTube as fallback
            "lyrics_providers": [],  # Disable lyrics providers to speed up
        }
    )
    logger.info("SpotDL initialized successfully with optimized settings")
except Exception as e:
    logger.error(f"Failed to initialize SpotDL: {str(e)}")
    spotdl = None


def validate_spotify_url(url):
    """Validate if the URL is a valid Spotify URL"""
    spotify_patterns = [
        r'^https?://open\.spotify\.com/track/[A-Za-z0-9]+',
        r'^https?://open\.spotify\.com/playlist/[A-Za-z0-9]+',
        r'^https?://open\.spotify\.com/album/[A-Za-z0-9]+',
        r'^spotify:track:[A-Za-z0-9]+',
        r'^spotify:playlist:[A-Za-z0-9]+',
        r'^spotify:album:[A-Za-z0-9]+',
    ]
    
    for pattern in spotify_patterns:
        if re.match(pattern, url):
            return True
    return False


def clean_filename(filename):
    """Clean filename to be filesystem-safe"""
    # Remove invalid characters
    filename = re.sub(r'[<>:"/\\|?*]', '', filename)
    # Remove leading/trailing spaces and dots
    filename = filename.strip('. ')
    # Limit length
    if len(filename) > 200:
        filename = filename[:200]
    return filename


def format_duration(seconds):
    """Format duration in seconds to MM:SS"""
    if not seconds:
        return "0:00"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}:{secs:02d}"


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "service": "SpotDown API",
        "spotdl_ready": spotdl is not None,
        "timestamp": datetime.now().isoformat()
    })


@app.route('/api/test-connection', methods=['GET'])
def test_connection():
    """Test API connection"""
    return jsonify({
        "success": True,
        "message": "Backend is running",
        "spotdl_status": "ready" if spotdl else "not initialized"
    })


@app.route('/api/extract', methods=['POST', 'OPTIONS'])
def extract_info():
    """Extract metadata from Spotify URL"""
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                "success": False,
                "error": "URL is required"
            }), 400
        
        url = data['url'].strip()
        
        # Validate URL
        if not validate_spotify_url(url):
            return jsonify({
                "success": False,
                "error": "Invalid Spotify URL. Please provide a valid track, album, or playlist URL."
            }), 400
        
        if not spotdl:
            return jsonify({
                "success": False,
                "error": "SpotDL is not initialized. Please check server configuration."
            }), 500
        
        logger.info(f"Extracting info for URL: {url}")
        
        # Search for the song using SpotDL
        songs = spotdl.search([url])
        
        if not songs or len(songs) == 0:
            return jsonify({
                "success": False,
                "error": "Could not find track information. The content might be unavailable in your region."
            }), 404
        
        # Get first song info
        song = songs[0]
        
        # Extract metadata
        response_data = {
            "success": True,
            "title": song.name,
            "artist": ", ".join(song.artists) if song.artists else "Unknown Artist",
            "album": song.album_name if hasattr(song, 'album_name') else None,
            "duration": song.duration,
            "mediaUrl": url,  # We'll use this for download reference
            "mediaType": "audio",
            "thumbnail": song.cover_url if hasattr(song, 'cover_url') else None,
            "albumArt": song.cover_url if hasattr(song, 'cover_url') else None,
            "quality": "320kbps",
            "year": song.year if hasattr(song, 'year') else None,
            "isrc": song.isrc if hasattr(song, 'isrc') else None
        }
        
        logger.info(f"Successfully extracted info: {song.name} by {', '.join(song.artists)}")
        
        return jsonify(response_data)
    
    except Exception as e:
        logger.error(f"Error extracting info: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Failed to extract track information: {str(e)}"
        }), 500


@app.route('/api/download', methods=['GET', 'OPTIONS'])
def download_track():
    """Download track from Spotify URL"""
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        url = request.args.get('url')
        if not url:
            return jsonify({
                "success": False,
                "error": "URL parameter is required"
            }), 400
        
        # Validate URL
        if not validate_spotify_url(url):
            return jsonify({
                "success": False,
                "error": "Invalid Spotify URL"
            }), 400
        
        if not spotdl:
            return jsonify({
                "success": False,
                "error": "SpotDL is not initialized"
            }), 500
        
        logger.info(f"Downloading track from URL: {url}")
        
        # Search and download
        songs = spotdl.search([url])
        
        if not songs or len(songs) == 0:
            return jsonify({
                "success": False,
                "error": "Track not found"
            }), 404
        
        song = songs[0]
        
        # Create a unique subdirectory for this download
        download_dir = os.path.join(TEMP_DIR, f"download_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}")
        os.makedirs(download_dir, exist_ok=True)
        
        # Update SpotDL output directory
        spotdl.downloader.settings["output"] = download_dir
        
        # Download the song with optimized settings and fallback providers
        logger.info(f"Starting download: {song.name} by {', '.join(song.artists)}")
        
        # Try download with fallback mechanism
        result = None
        path = None
        last_error = None
        
        # Define provider fallback order with more options
        provider_configs = [
            {"audio_providers": ["youtube-music"], "description": "YouTube Music (Primary)"},
            {"audio_providers": ["youtube"], "description": "YouTube (Standard)"},
            {"audio_providers": ["soundcloud"], "description": "SoundCloud"},
            {"audio_providers": ["bandcamp"], "description": "Bandcamp"},
            {"audio_providers": ["youtube-music", "youtube"], "description": "YouTube Music + YouTube"},
            {"audio_providers": ["youtube", "youtube-music", "soundcloud"], "description": "All YouTube + SoundCloud"},
        ]
        
        for i, config in enumerate(provider_configs):
            try:
                logger.info(f"Attempt {i+1}/{len(provider_configs)}: Trying {config['description']}")
                
                # Update provider configuration
                spotdl.downloader.settings["audio_providers"] = config["audio_providers"]
                
                # Attempt download
                result, path = spotdl.downloader.search_and_download(song)
                
                if path and os.path.exists(path):
                    logger.info(f"✓ Successfully downloaded using {config['description']}")
                    break
                else:
                    logger.warning(f"✗ Download failed with {config['description']} - no file created")
                    last_error = f"No file created with {config['description']}"
                    
            except Exception as download_error:
                error_msg = str(download_error)
                logger.warning(f"✗ Download failed with {config['description']}: {error_msg}")
                last_error = error_msg
                
                # If this is a YT-DLP error, try the next provider
                if "YT-DLP" in error_msg or "youtube" in error_msg.lower():
                    continue
                else:
                    # For other errors, might still try next provider
                    continue
        
        logger.info(f"Download result: {result}, Path: {path}")
        
        if not path or not os.path.exists(path):
            error_message = f"Download failed after trying all providers. Last error: {last_error}"
            logger.error(error_message)
            return jsonify({
                "success": False,
                "error": error_message
            }), 500
        
        # Create safe filename
        safe_artist = clean_filename(", ".join(song.artists)[:50])
        safe_title = clean_filename(song.name[:50])
        filename = f"{safe_artist} - {safe_title}.mp3"
        
        logger.info(f"Successfully downloaded to: {path}")
        
        # Send file
        response = send_file(
            path,
            as_attachment=True,
            download_name=filename,
            mimetype='audio/mpeg'
        )
        
        # Add CORS headers
        response.headers['Access-Control-Allow-Origin'] = request.headers.get('Origin', '*')
        response.headers['Access-Control-Expose-Headers'] = 'Content-Disposition'
        
        # Clean up after sending (using after_request hook would be better for production)
        try:
            # Schedule cleanup after a delay
            import threading
            def cleanup():
                import time
                time.sleep(5)  # Wait 5 seconds before cleanup
                try:
                    if os.path.exists(path):
                        os.remove(path)
                    if os.path.exists(download_dir):
                        shutil.rmtree(download_dir, ignore_errors=True)
                except Exception as e:
                    logger.error(f"Cleanup error: {str(e)}")
            
            threading.Thread(target=cleanup, daemon=True).start()
        except Exception as e:
            logger.error(f"Failed to schedule cleanup: {str(e)}")
        
        return response
    
    except Exception as e:
        logger.error(f"Error downloading track: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Failed to download track: {str(e)}"
        }), 500


@app.route('/api/playlist', methods=['POST', 'OPTIONS'])
def extract_playlist():
    """Extract playlist information using Spotipy"""
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({
                "success": False,
                "error": "URL is required"
            }), 400
        
        url = data['url'].strip()
        
        if not spotify_client:
            return jsonify({
                "success": False,
                "error": "Spotify client is not initialized"
            }), 500
        
        logger.info(f"Extracting playlist/album info for URL: {url}")
        
        # Extract Spotify ID from URL
        playlist_id = None
        album_id = None
        is_playlist = False
        
        # Parse URL to extract ID
        # Handle formats like:
        # https://open.spotify.com/playlist/37i9dQZF1E4oJSdHZrVjxD
        # https://open.spotify.com/playlist/37i9dQZF1E4oJSdHZrVjxD?si=xxx
        if '/playlist/' in url:
            is_playlist = True
            # Extract ID after /playlist/
            parts = url.split('/playlist/')[-1].split('?')[0].split('/')
            playlist_id = parts[0].strip()
            logger.info(f"Extracted playlist ID: {playlist_id}")
        elif '/album/' in url:
            # Extract ID after /album/
            parts = url.split('/album/')[-1].split('?')[0].split('/')
            album_id = parts[0].strip()
            logger.info(f"Extracted album ID: {album_id}")
        else:
            return jsonify({
                "success": False,
                "error": "Invalid playlist or album URL"
            }), 400
        
        # Validate ID format (Spotify IDs are 22 characters long)
        extracted_id = playlist_id if is_playlist else album_id
        if not extracted_id or len(extracted_id) < 20:
            return jsonify({
                "success": False,
                "error": f"Invalid Spotify ID extracted: {extracted_id}"
            }), 400
        
        tracks = []
        playlist_name = None
        playlist_cover = None
        
        if is_playlist:
            # Fetch playlist using Spotipy
            logger.info(f"Fetching playlist: {playlist_id}")
            try:
                # Try to get basic playlist info first
                playlist = spotify_client.playlist(playlist_id, fields="name,images,tracks.total,public,collaborative")
                
                # Check if playlist is accessible
                if not playlist:
                    raise spotipy.exceptions.SpotifyException(404, -1, "Playlist not accessible")
                
                playlist_name = playlist.get('name', 'Unknown Playlist')
                if playlist.get('images') and len(playlist['images']) > 0:
                    playlist_cover = playlist['images'][0]['url']
                
                # Log playlist info
                logger.info(f"Playlist found: '{playlist_name}', Total tracks: {playlist.get('tracks', {}).get('total', 'unknown')}")
                
                # Check if playlist is public or accessible
                if playlist.get('public') is False and playlist.get('collaborative') is False:
                    logger.warning(f"Playlist '{playlist_name}' is private, attempting SpotDL fallback")
                    raise spotipy.exceptions.SpotifyException(403, -1, "Private playlist")
                
                # Get all tracks (handle pagination)
                logger.info("Fetching playlist tracks...")
                results = spotify_client.playlist_tracks(playlist_id, fields="items(track(id,name,artists,duration_ms,external_urls,album(images))),next")
                playlist_tracks = results['items']
                
                page_count = 1
                while results['next']:
                    page_count += 1
                    logger.info(f"Fetching page {page_count} of playlist tracks...")
                    results = spotify_client.next(results)
                    playlist_tracks.extend(results['items'])
                
                logger.info(f"Successfully fetched {len(playlist_tracks)} tracks from Spotipy")
                
                # Process tracks
                valid_tracks = 0
                for item in playlist_tracks:
                    if item and item.get('track') and item['track'].get('id'):
                        track = item['track']
                        artists = ", ".join([artist['name'] for artist in track.get('artists', [])])
                        
                        tracks.append({
                            "title": track.get('name', 'Unknown Title'),
                            "artist": artists or 'Unknown Artist',
                            "duration": track.get('duration_ms', 0) / 1000,  # Convert to seconds
                            "url": track.get('external_urls', {}).get('spotify', ''),
                            "cover": track.get('album', {}).get('images', [{}])[0].get('url') if track.get('album', {}).get('images') else None
                        })
                        valid_tracks += 1
                    else:
                        logger.warning("Skipped invalid or null track")
                
                logger.info(f"Processed {valid_tracks} valid tracks out of {len(playlist_tracks)} total items")
                
            except spotipy.exceptions.SpotifyException as spotify_error:
                # If Spotipy fails (404, region-restricted, rate limited, etc.), fallback to SpotDL
                error_msg = str(spotify_error)
                status_code = getattr(spotify_error, 'http_status', 0)
                
                if status_code == 429 or "429" in error_msg or "Max Retries" in error_msg:
                    logger.warning(f"Spotipy rate limited for playlist, using SpotDL fallback: {error_msg}")
                elif status_code == 404 or "404" in error_msg:
                    logger.warning(f"Playlist not found via Spotipy (possibly region-restricted), using SpotDL fallback: {error_msg}")
                elif status_code == 403 or "403" in error_msg:
                    logger.warning(f"Playlist access denied via Spotipy (private playlist), using SpotDL fallback: {error_msg}")
                else:
                    logger.warning(f"Spotipy failed for playlist (status: {status_code}), falling back to SpotDL: {error_msg}")
                
                if not spotdl:
                    raise Exception("Both Spotipy and SpotDL are unavailable")
                
                # Use SpotDL as fallback
                logger.info("Attempting to fetch playlist using SpotDL...")
                try:
                    songs = spotdl.search([url])
                    
                    if not songs or len(songs) == 0:
                        # Try alternative URL formats
                        logger.info("Trying alternative URL formats with SpotDL...")
                        alt_urls = [
                            url.split('?')[0],  # Remove query parameters
                            f"spotify:playlist:{playlist_id}",  # Spotify URI format
                        ]
                        
                        for alt_url in alt_urls:
                            logger.info(f"Trying URL: {alt_url}")
                            songs = spotdl.search([alt_url])
                            if songs and len(songs) > 0:
                                break
                    
                    if not songs or len(songs) == 0:
                        raise Exception("Playlist not found or is empty in both Spotipy and SpotDL")
                    
                    # Extract metadata from SpotDL results
                    playlist_name = "Spotify Playlist"  # Default name
                    if songs and len(songs) > 0:
                        first_song = songs[0]
                        if hasattr(first_song, 'album_name') and first_song.album_name:
                            playlist_name = first_song.album_name
                        elif hasattr(first_song, 'playlist_name') and first_song.playlist_name:
                            playlist_name = first_song.playlist_name
                        if hasattr(first_song, 'cover_url'):
                            playlist_cover = first_song.cover_url
                    
                    # Process SpotDL songs
                    for song in songs:
                        if song and hasattr(song, 'name'):
                            tracks.append({
                                "title": song.name,
                                "artist": ", ".join(song.artists) if hasattr(song, 'artists') and song.artists else "Unknown Artist",
                                "duration": song.duration if hasattr(song, 'duration') else 0,
                                "url": song.url if hasattr(song, 'url') else url,
                                "cover": song.cover_url if hasattr(song, 'cover_url') else None
                            })
                    
                    logger.info(f"Successfully fetched playlist using SpotDL fallback: {len(tracks)} tracks")
                    
                except Exception as spotdl_error:
                    logger.error(f"SpotDL also failed: {str(spotdl_error)}")
                    
                    # Third tier fallback: Try Spotify Scraper for editorial/restricted playlists
                    if scraper_client:
                        logger.info("Attempting Spotify Scraper fallback for playlist...")
                        try:
                            playlist_url = url.split('?')[0]  # Clean URL
                            scraped_data = scraper_client.get_playlist_info(playlist_url)
                            
                            if scraped_data and scraped_data.get('name'):
                                logger.info(f"Scraper successfully accessed playlist: {scraped_data.get('name')}")
                                logger.info(f"Scraper data keys: {list(scraped_data.keys())}")
                                
                                # Debug: Log first track structure if available
                                if scraped_data.get('tracks') and len(scraped_data.get('tracks', [])) > 0:
                                    first_track = scraped_data['tracks'][0]
                                    logger.info(f"First track keys: {list(first_track.keys())}")
                                    if first_track.get('album'):
                                        logger.info(f"Album keys: {list(first_track['album'].keys()) if isinstance(first_track['album'], dict) else type(first_track['album'])}")
                                
                                playlist_name = scraped_data.get('name', 'Unknown Playlist')
                                playlist_cover = scraped_data.get('cover') or scraped_data.get('image') or scraped_data.get('images', [{}])[0].get('url') if scraped_data.get('images') else None
                                
                                logger.info(f"Playlist cover URL: {playlist_cover}")
                                
                                # Process scraped tracks with concurrent image fetching
                                scraped_tracks = scraped_data.get('tracks', [])
                                
                                # Get all track URIs for concurrent image fetching
                                track_uris = [track.get('uri', '') for track in scraped_tracks if track.get('uri')]
                                logger.info(f"Fetching images for {len(track_uris)} tracks concurrently...")
                                
                                # Fetch all images concurrently using async
                                track_images = {}
                                if track_uris:
                                    try:
                                        track_images = asyncio.run(fetch_all_track_images(track_uris))
                                        logger.info(f"✓ Successfully fetched {len([img for img in track_images.values() if img])} images out of {len(track_uris)} tracks")
                                    except Exception as e:
                                        logger.warning(f"✗ Error in concurrent image fetching: {str(e)}")
                                        track_images = {}
                                
                                # Process scraped tracks with fetched images
                                for idx, track in enumerate(scraped_tracks):
                                    # Handle artists (can be list of dicts or strings)
                                    artists_list = []
                                    if isinstance(track.get('artists'), list):
                                        artists_list = [
                                            a.get('name', 'Unknown') if isinstance(a, dict) else str(a) 
                                            for a in track.get('artists', [])
                                        ]
                                    elif isinstance(track.get('artists'), str):
                                        artists_list = [track.get('artists')]
                                    
                                    artist_names = ", ".join(artists_list) if artists_list else "Unknown Artist"
                                    
                                    # Get track image from concurrent fetch first
                                    track_uri = track.get('uri', '')
                                    track_cover = track_images.get(track_uri)
                                    
                                    # Fallback to album image if concurrent fetch didn't work
                                    if not track_cover and track.get('album'):
                                        album = track['album']
                                        if isinstance(album, dict):
                                            track_cover = (
                                                album.get('image') or 
                                                album.get('cover') or 
                                                album.get('images', [{}])[0].get('url') if album.get('images') else None
                                            )
                                    
                                    # Fallback: check track-level image fields
                                    if not track_cover:
                                        track_cover = (
                                            track.get('image') or 
                                            track.get('cover') or 
                                            track.get('album_art') or
                                            track.get('images', [{}])[0].get('url') if track.get('images') else None
                                        )
                                    
                                    # Final fallback: use playlist cover
                                    if not track_cover:
                                        track_cover = playlist_cover
                                    
                                    if idx < 3:
                                        logger.info(f"Track {idx+1} '{track.get('name')}' cover: {track_cover}")
                                    
                                    duration_ms = track.get('duration_ms', 0)
                                    track_id = track.get('id')
                                    
                                    # If ID is empty, try to extract from URI
                                    if not track_id and track.get('uri'):
                                        import re
                                        uri_match = re.search(r'track:([a-zA-Z0-9]+)', track.get('uri', ''))
                                        if uri_match:
                                            track_id = uri_match.group(1)
                                    
                                    tracks.append({
                                        "title": track.get('name', 'Unknown'),
                                        "artist": artist_names,
                                        "duration": duration_ms / 1000 if duration_ms else 0,
                                        "url": f"https://open.spotify.com/track/{track_id}" if track_id else '',
                                        "cover": track_cover
                                    })
                                
                                logger.info(f"Successfully scraped playlist with {len(tracks)} tracks")
                                
                                # If we got tracks, return success
                                if tracks:
                                    return jsonify({
                                        "success": True,
                                        "totalTracks": len(tracks),
                                        "tracks": tracks,
                                        "name": playlist_name,
                                        "cover": playlist_cover,
                                        "source": "Scraper"
                                    })
                            else:
                                logger.warning("Scraper returned empty or invalid data")
                        
                        except Exception as scraper_error:
                            logger.error(f"Spotify Scraper also failed: {str(scraper_error)}")
                    
                    # Last resort: Try to extract individual tracks if it's a known playlist format
                    logger.info("Attempting last resort: checking if playlist URL can be processed differently...")
                    
                    # Check if it's a Spotify-generated playlist (like Discover Weekly, Daily Mix, Radio, etc.)
                    if "37i9dQZF1" in playlist_id:
                        logger.warning("This appears to be a Spotify-generated playlist (Radio, Daily Mix, Discover Weekly, etc.) which may be user-specific and not publicly accessible")
                        
                        # Try one more approach for radio playlists - sometimes they work with different API calls
                        logger.info("Attempting special handling for Spotify Radio playlist...")
                        try:
                            # Try to get just basic info first
                            basic_info = spotify_client.playlist(playlist_id, fields="name,description,images")
                            if basic_info and 'radio' in basic_info.get('name', '').lower():
                                logger.info(f"Confirmed this is a radio playlist: {basic_info.get('name')}")
                            
                            # Sometimes radio playlists work with different field requests
                            tracks_only = spotify_client.playlist_tracks(playlist_id, fields="items(track(id,name,artists,duration_ms,external_urls,album))", limit=50)
                            
                            if tracks_only and tracks_only.get('items'):
                                playlist_name = basic_info.get('name', 'Spotify Radio')
                                if basic_info.get('images'):
                                    playlist_cover = basic_info['images'][0]['url']
                                
                                logger.info(f"Successfully accessed radio playlist with {len(tracks_only['items'])} tracks")
                                
                                for item in tracks_only['items']:
                                    if item and item.get('track') and item['track'].get('id'):
                                        track = item['track']
                                        artists = ", ".join([artist['name'] for artist in track.get('artists', [])])
                                        tracks.append({
                                            "title": track.get('name', 'Unknown Title'),
                                            "artist": artists or 'Unknown Artist',
                                            "duration": track.get('duration_ms', 0) / 1000,
                                            "url": track.get('external_urls', {}).get('spotify', ''),
                                            "cover": track.get('album', {}).get('images', [{}])[0].get('url') if track.get('album', {}).get('images') else None
                                        })
                                
                                if tracks:
                                    logger.info(f"Successfully processed radio playlist: {len(tracks)} tracks")
                                    return jsonify({
                                        "success": True,
                                        "totalTracks": len(tracks),
                                        "tracks": tracks,
                                        "name": playlist_name,
                                        "cover": playlist_cover,
                                        "source": "API (Radio Special)"
                                    })
                            
                        except Exception as radio_error:
                            logger.error(f"Radio playlist special handling failed: {str(radio_error)}")
                        
                        raise Exception("This appears to be a Spotify Radio playlist from the 'Popular Radio' section. These playlists are often:\n1. Region-specific and may not be available in your location\n2. Algorithmically generated and change frequently\n3. May have restricted access outside of the Spotify app\n\nTry using a regular user-created public playlist instead.")
                    
                    # Check if it's a very long playlist ID (sometimes URLs get malformed)
                    if len(playlist_id) > 25:
                        logger.info(f"Playlist ID seems too long ({len(playlist_id)} chars), trying to extract proper ID...")
                        # Try to find a proper 22-character Spotify ID within the string
                        import re
                        proper_id_match = re.search(r'[A-Za-z0-9]{22}', playlist_id)
                        if proper_id_match:
                            corrected_id = proper_id_match.group()
                            logger.info(f"Found potential proper ID: {corrected_id}, retrying...")
                            try:
                                # Try with corrected ID
                                corrected_playlist = spotify_client.playlist(corrected_id, fields="name,images,tracks.total,public,collaborative")
                                if corrected_playlist:
                                    logger.info("Corrected ID worked! Processing with proper ID...")
                                    playlist_name = corrected_playlist.get('name', 'Unknown Playlist')
                                    if corrected_playlist.get('images'):
                                        playlist_cover = corrected_playlist['images'][0]['url']
                                    
                                    # Get tracks with corrected ID
                                    track_results = spotify_client.playlist_tracks(corrected_id)
                                    for item in track_results['items']:
                                        if item and item.get('track') and item['track'].get('id'):
                                            track = item['track']
                                            artists = ", ".join([artist['name'] for artist in track.get('artists', [])])
                                            tracks.append({
                                                "title": track.get('name', 'Unknown Title'),
                                                "artist": artists or 'Unknown Artist',
                                                "duration": track.get('duration_ms', 0) / 1000,
                                                "url": track.get('external_urls', {}).get('spotify', ''),
                                                "cover": track.get('album', {}).get('images', [{}])[0].get('url')
                                            })
                                    
                                    if tracks:
                                        logger.info(f"Successfully processed corrected playlist: {len(tracks)} tracks")
                                        return jsonify({
                                            "success": True,
                                            "totalTracks": len(tracks),
                                            "tracks": tracks,
                                            "name": playlist_name,
                                            "cover": playlist_cover,
                                            "source": "API (Corrected ID)"
                                        })
                            except Exception as corrected_error:
                                logger.error(f"Corrected ID also failed: {str(corrected_error)}")
                    
                    raise Exception(f"Playlist unavailable through all methods (Spotipy, SpotDL, Scraper). This playlist may be:\n1. Region-restricted or not available in your country\n2. Private or deleted\n3. A personalized playlist (Daily Mix, Discover Weekly) only accessible to the owner\n4. Temporarily unavailable due to Spotify restrictions\n\nSpotipy error: {error_msg}\nSpotDL error: {str(spotdl_error)}")
                
            except Exception as general_error:
                logger.error(f"Unexpected error with Spotipy: {str(general_error)}")
                # Still try SpotDL as fallback for any other errors
                if not spotdl:
                    raise Exception(f"Spotipy failed with unexpected error and SpotDL unavailable: {str(general_error)}")
                
                logger.info("Attempting SpotDL fallback due to unexpected Spotipy error...")
                try:
                    songs = spotdl.search([url])
                    if songs and len(songs) > 0:
                        for song in songs:
                            tracks.append({
                                "title": song.name,
                                "artist": ", ".join(song.artists) if hasattr(song, 'artists') and song.artists else "Unknown Artist",
                                "duration": song.duration if hasattr(song, 'duration') else 0,
                                "url": song.url if hasattr(song, 'url') else url,
                                "cover": song.cover_url if hasattr(song, 'cover_url') else None
                            })
                        logger.info(f"SpotDL fallback successful: {len(tracks)} tracks")
                    else:
                        raise Exception("No tracks found with SpotDL either")
                except Exception as spotdl_error:
                    raise Exception(f"Both methods failed. Spotipy: {str(general_error)}, SpotDL: {str(spotdl_error)}")
        else:
            # Fetch album using Spotipy
            logger.info(f"Fetching album: {album_id}")
            try:
                album = spotify_client.album(album_id)
                
                playlist_name = album['name']
                if album['images'] and len(album['images']) > 0:
                    playlist_cover = album['images'][0]['url']
                
                # Get all tracks
                album_tracks = album['tracks']['items']
                
                # Process tracks
                for track in album_tracks:
                    artists = ", ".join([artist['name'] for artist in track['artists']])
                    
                    tracks.append({
                        "title": track['name'],
                        "artist": artists,
                        "duration": track['duration_ms'] / 1000,  # Convert to seconds
                        "url": track['external_urls']['spotify'],
                        "cover": playlist_cover  # Use album cover for all tracks
                    })
            except spotipy.exceptions.SpotifyException as spotify_error:
                # If Spotipy fails, fallback to SpotDL
                error_msg = str(spotify_error)
                status_code = getattr(spotify_error, 'http_status', 0)
                
                logger.warning(f"Spotipy failed for album (status: {status_code}), falling back to SpotDL: {error_msg}")
                
                if not spotdl:
                    raise Exception("Both Spotipy and SpotDL are unavailable")
                
                # Use SpotDL as fallback
                logger.info("Attempting to fetch album using SpotDL...")
                try:
                    songs = spotdl.search([url])
                    
                    if not songs or len(songs) == 0:
                        # Try alternative URL formats
                        alt_urls = [
                            url.split('?')[0],
                            f"spotify:album:{album_id}",
                        ]
                        
                        for alt_url in alt_urls:
                            songs = spotdl.search([alt_url])
                            if songs and len(songs) > 0:
                                break
                    
                    if not songs or len(songs) == 0:
                        raise Exception("Album not found or is empty in both Spotipy and SpotDL")
                    
                    # Extract metadata from SpotDL results
                    playlist_name = "Spotify Album"
                    if songs and len(songs) > 0:
                        first_song = songs[0]
                        if hasattr(first_song, 'album_name') and first_song.album_name:
                            playlist_name = first_song.album_name
                        if hasattr(first_song, 'cover_url'):
                            playlist_cover = first_song.cover_url
                    
                    # Process SpotDL songs
                    for song in songs:
                        if song and hasattr(song, 'name'):
                            tracks.append({
                                "title": song.name,
                                "artist": ", ".join(song.artists) if hasattr(song, 'artists') and song.artists else "Unknown Artist",
                                "duration": song.duration if hasattr(song, 'duration') else 0,
                                "url": song.url if hasattr(song, 'url') else url,
                                "cover": song.cover_url if hasattr(song, 'cover_url') else playlist_cover
                            })
                    
                    logger.info(f"Successfully fetched album using SpotDL fallback: {len(tracks)} tracks")
                    
                except Exception as spotdl_error:
                    logger.error(f"SpotDL also failed for album: {str(spotdl_error)}")
                    raise Exception(f"Both Spotipy and SpotDL failed for album. Spotipy: {error_msg}, SpotDL: {str(spotdl_error)}")
        
        response_data = {
            "success": True,
            "totalTracks": len(tracks),
            "tracks": tracks,
            "name": playlist_name,
            "cover": playlist_cover
        }
        
        logger.info(f"Successfully extracted {len(tracks)} tracks from '{playlist_name}' using Spotipy")
        
        return jsonify(response_data)
    
    except spotipy.exceptions.SpotifyException as e:
        logger.error(f"Spotify API error: {str(e)}")
        return jsonify({
            "success": False,
            "error": f"Spotify API error: {str(e)}"
        }), 500
    except Exception as e:
        logger.error(f"Error extracting playlist: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Failed to extract playlist: {str(e)}"
        }), 500


@app.route('/api/cut-audio', methods=['POST', 'OPTIONS'])
def cut_audio():
    """Cut/trim audio file based on start and end time"""
    if request.method == 'OPTIONS':
        return '', 204
    
    try:
        start_time = float(request.form.get('start_time', 0))
        end_time = float(request.form.get('end_time', 0))
        
        if start_time >= end_time:
            return jsonify({
                "success": False,
                "error": "Start time must be less than end time"
            }), 400
        
        # Get audio file or URL
        audio_file = request.files.get('audio')
        audio_url = request.form.get('url')
        
        if not audio_file and not audio_url:
            return jsonify({
                "success": False,
                "error": "No audio file or URL provided"
            }), 400
        
        # Import pydub for audio processing
        try:
            from pydub import AudioSegment
        except ImportError:
            return jsonify({
                "success": False,
                "error": "Audio processing library not installed. Please install pydub and ffmpeg."
            }), 500
        
        # Create a temporary directory for this operation
        temp_dir = tempfile.mkdtemp(prefix="audio_cut_")
        
        try:
            # Load audio
            if audio_file:
                # Save uploaded file
                input_path = os.path.join(temp_dir, "input_audio")
                audio_file.save(input_path)
                audio = AudioSegment.from_file(input_path)
            else:
                # Download from URL
                import requests
                response = requests.get(audio_url, stream=True)
                input_path = os.path.join(temp_dir, "input_audio")
                with open(input_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        f.write(chunk)
                audio = AudioSegment.from_file(input_path)
            
            # Convert seconds to milliseconds for pydub
            start_ms = int(start_time * 1000)
            end_ms = int(end_time * 1000)
            
            # Cut the audio
            cut_audio = audio[start_ms:end_ms]
            
            # Export cut audio
            output_path = os.path.join(temp_dir, "output_audio.mp3")
            cut_audio.export(output_path, format="mp3", bitrate="320k")
            
            # Send the file
            return send_file(
                output_path,
                mimetype='audio/mpeg',
                as_attachment=True,
                download_name=f'cut_audio_{int(start_time)}s_to_{int(end_time)}s.mp3'
            )
        
        finally:
            # Cleanup will happen after file is sent
            # Schedule cleanup in a background thread if needed
            pass
    
    except Exception as e:
        logger.error(f"Error cutting audio: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            "success": False,
            "error": f"Failed to cut audio: {str(e)}"
        }), 500


@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "success": False,
        "error": "Endpoint not found"
    }), 404


@app.errorhandler(500)
def internal_error(e):
    return jsonify({
        "success": False,
        "error": "Internal server error"
    }), 500


if __name__ == '__main__':
    logger.info("=" * 60)
    logger.info("Starting SpotDown Backend Server")
    logger.info("=" * 60)
    logger.info(f"Temporary directory: {TEMP_DIR}")
    logger.info(f"SpotDL status: {'Ready' if spotdl else 'Not initialized'}")
    logger.info(f"Spotipy status: {'Ready' if spotify_client else 'Not initialized'}")
    logger.info(f"Spotify Scraper status: {'Ready' if scraper_client else 'Not initialized'}")
    logger.info("Fallback chain: Spotipy API → SpotDL → Spotify Scraper → Special Handlers")
    logger.info("Server running on http://localhost:5000")
    logger.info("=" * 60)
    
    try:
        app.run(host='0.0.0.0', port=5000, debug=True)
    finally:
        # Cleanup temp directory on shutdown
        try:
            if os.path.exists(TEMP_DIR):
                shutil.rmtree(TEMP_DIR, ignore_errors=True)
                logger.info(f"Cleaned up temporary directory: {TEMP_DIR}")
        except Exception as e:
            logger.error(f"Failed to cleanup temp directory: {str(e)}")
