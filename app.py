import os
import subprocess
import logging
from flask import Flask, jsonify, request, send_from_directory, send_file
from threading import Lock
from datetime import datetime
import cv2
from pathlib import Path
import time

app = Flask(__name__)

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global dictionary to store active FFmpeg processes for each camera.
# Structure:
#   active_ffmpeg_processes = {
#       "0": { "main": <subprocess.Popen>, "hls": <subprocess.Popen> or None },
#       "1": { "main": <...>, "hls": <...> },
#       ...
#   }
active_ffmpeg_processes = {}
process_lock = Lock()  # Ensure thread-safe access to the above dictionary

# Add these constants at the top
MAX_CAMERAS = 10
RECORDINGS_DIR = "recordings"

###############################################################################
# Helper functions to build FFmpeg commands
###############################################################################

def build_ffmpeg_command(camera_id, outputs):
    """Build FFmpeg command for reliable streaming"""
    hls_directory = ensure_hls_directory(camera_id)
    playlist_path = os.path.join(hls_directory, "playlist.m3u8")

    return [
        "ffmpeg",
        "-y",
        # Input options
        "-f", "v4l2",
        "-input_format", "mjpeg",  # Try MJPEG first
        "-video_size", "640x480",
        "-framerate", "30",
        "-i", f"/dev/video{camera_id}",
        
        # Simple encoding options
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-g", "30",
        "-b:v", "2000k",
        "-bufsize", "4000k",
        "-maxrate", "2000k",
        
        # HLS options
        "-f", "hls",
        "-hls_time", "1",
        "-hls_list_size", "3",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", f"{hls_directory}/segment%03d.ts",
        playlist_path
    ]

def ensure_hls_directory(camera_id):
    """Ensure HLS directory exists with proper permissions"""
    hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
    try:
        # Create directory with parents if it doesn't exist
        Path(hls_directory).mkdir(parents=True, exist_ok=True)
        # Set directory permissions to 755
        os.chmod(hls_directory, 0o755)
        # Create an empty playlist file to ensure write permissions
        playlist_path = os.path.join(hls_directory, "playlist.m3u8")
        Path(playlist_path).touch()
        os.chmod(playlist_path, 0o644)
        return hls_directory
    except Exception as e:
        logger.error(f"Failed to create HLS directory: {str(e)}")
        raise

def build_hls_command(camera_id, outputs):
    """Build the HLS FFmpeg command with ultra-low latency settings"""
    hls_conf = outputs.get("hls", {})
    if not hls_conf.get("enabled", False):
        return None

    hls_directory = ensure_hls_directory(camera_id)
    playlist_path = os.path.join(hls_directory, "playlist.m3u8")

    return [
        "ffmpeg",
        "-y",
        # Input options - Using MJPEG for better performance
        "-f", "v4l2",
        "-input_format", "mjpeg",  # Changed from yuyv422 to mjpeg
        "-video_size", "640x480",
        "-framerate", "30",
        "-thread_queue_size", "512",  # Reduced buffer size
        "-i", f"/dev/video{camera_id}",
        # Ultra low-latency encoding options
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-g", "10",             # Reduced GOP size
        "-sc_threshold", "0",
        "-b:v", "800k",        # Slightly reduced bitrate
        "-maxrate", "800k",
        "-bufsize", "400k",     # Reduced buffer size
        "-pix_fmt", "yuv420p",
        "-profile:v", "baseline",
        "-level", "3.0",
        "-fps_mode", "vfr",     # Variable framerate mode
        # HLS specific options for ultra-low latency
        "-f", "hls",
        "-hls_time", "0.2",     # Very short segments
        "-hls_list_size", "2",  # Keep only 2 segments
        "-hls_flags", "delete_segments+append_list+omit_endlist+discont_start",
        "-hls_segment_type", "mpegts",
        "-hls_start_number_source", "datetime",
        "-start_number", "1",
        "-hls_segment_filename", f"{hls_directory}/segment%03d.ts",
        playlist_path
    ]

def build_recording_command(camera_id, outputs):
    """Build command for continuous recording"""
    rec_conf = outputs.get("recording", {})
    if not rec_conf.get("enabled", False):
        return None

    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    filename = f"camera_{camera_id}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.mp4"
    filepath = os.path.join(RECORDINGS_DIR, filename)

    return [
        "ffmpeg",
        "-y",
        "-f", "v4l2",
        "-i", f"/dev/video{camera_id}",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-c:a", "aac",  # Missing audio support
        "-f", "mp4",
        filepath
    ]

def verify_camera_access(camera_id):
    """Verify that the camera exists and is accessible"""
    try:
        device_path = f"/dev/video{camera_id}"
        if not os.path.exists(device_path):
            return False, f"Camera device {device_path} not found"
            
        # Try to open the camera with OpenCV
        cap = cv2.VideoCapture(int(camera_id))
        if not cap.isOpened():
            return False, "Failed to open camera with OpenCV"
            
        # Try to read a frame
        ret, frame = cap.read()
        cap.release()
        
        if not ret or frame is None:
            return False, "Failed to read frame from camera"
            
        logger.info(f"Successfully verified camera {camera_id}")
        return True, "Camera is accessible"
        
    except Exception as e:
        return False, f"Error accessing camera: {str(e)}"

def test_camera_capture(camera_id):
    """Test camera capture and determine working format"""
    try:
        cap = cv2.VideoCapture(int(camera_id))
        if not cap.isOpened():
            raise Exception(f"Failed to open camera {camera_id}")

        # Try to set YUYV format
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('Y', 'U', 'Y', 'V'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 30)

        ret, frame = cap.read()
        if not ret:
            raise Exception(f"Failed to capture frame from camera {camera_id}")

        # Get actual format being used
        format_code = int(cap.get(cv2.CAP_PROP_FOURCC))
        format_name = "".join([chr((format_code >> 8 * i) & 0xFF) for i in range(4)])

        cap.release()
        return format_name.lower()

    except Exception as e:
        logger.error(f"Error testing camera {camera_id}: {e}")
        raise

def monitor_ffmpeg_output(process, camera_id):
    """Monitor FFmpeg process output for errors"""
    def reader():
        while True:
            line = process.stderr.readline()
            if not line:
                break
            line = line.strip()
            if line:
                logger.info(f"FFmpeg camera {camera_id}: {line}")
                # Check for critical errors
                if "Error" in line or "error" in line:
                    logger.error(f"FFmpeg error for camera {camera_id}: {line}")
                    # Kill the process on critical error
                    process.kill()
                    break
    
    from threading import Thread
    Thread(target=reader, daemon=True).start()

def cleanup_hls_files(camera_id):
    """Clean up HLS files before starting new stream"""
    try:
        hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
        if os.path.exists(hls_directory):
            for file in os.listdir(hls_directory):
                try:
                    os.remove(os.path.join(hls_directory, file))
                except Exception as e:
                    logger.warning(f"Failed to remove file: {e}")
        
        # Recreate directory
        Path(hls_directory).mkdir(parents=True, exist_ok=True)
        os.chmod(hls_directory, 0o755)
        
    except Exception as e:
        logger.error(f"Error cleaning up HLS files: {e}")

def release_camera(camera_id):
    """Force release the camera if it's in use"""
    try:
        # Try to open and immediately close the camera to force release
        cap = cv2.VideoCapture(int(camera_id))
        if cap.isOpened():
            cap.release()
        
        # Additional cleanup using v4l2-ctl if available
        try:
            subprocess.run(['v4l2-ctl', '--device', f'/dev/video{camera_id}', '--stream-mmap', '--stream-off'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1)
        except:
            pass
            
    except Exception as e:
        logger.warning(f"Error while trying to release camera {camera_id}: {e}")

def get_camera_capabilities(camera_id):
    """Get camera capabilities using v4l2-ctl"""
    try:
        result = subprocess.run(
            ['v4l2-ctl', '--device', f'/dev/video{camera_id}', '--list-formats-ext'],
            capture_output=True,
            text=True
        )
        logger.info(f"Camera {camera_id} capabilities: {result.stdout}")
        return result.stdout
    except Exception as e:
        logger.error(f"Error getting camera capabilities: {e}")
        return None

def ensure_camera_format(camera_id):
    """Ensure camera is set to a compatible format"""
    try:
        # Try to set YUYV format
        subprocess.run([
            'v4l2-ctl',
            '--device', f'/dev/video{camera_id}',
            '--set-fmt-video=width=640,height=480,pixelformat=YUYV'
        ], check=True)
        
        # Set frame rate
        subprocess.run([
            'v4l2-ctl',
            '--device', f'/dev/video{camera_id}',
            '--set-parm=30'
        ], check=True)
    except Exception as e:
        logger.error(f"Error setting camera format: {e}")

def cleanup_old_segments(camera_id):
    """Clean up old HLS segments to prevent disk space issues"""
    try:
        hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
        if os.path.exists(hls_directory):
            # Get all .ts files
            segments = sorted(Path(hls_directory).glob("segment*.ts"))
            
            # Keep only the latest 10 segments
            segments_to_delete = segments[:-10] if len(segments) > 10 else []
            
            for segment in segments_to_delete:
                try:
                    os.remove(segment)
                except Exception as e:
                    logger.warning(f"Failed to remove old segment {segment}: {e}")
                    
    except Exception as e:
        logger.error(f"Error cleaning up segments for camera {camera_id}: {e}")

def is_camera_available(camera_id):
    """Check if the camera is available and not in use"""
    try:
        device_path = f"/dev/video{camera_id}"
        cap = cv2.VideoCapture(camera_id)
        if cap.isOpened():
            cap.release()
            return True
        return False
    except Exception as e:
        logger.error(f"Error checking camera {camera_id}: {e}")
        return False

def verify_video_segments(camera_id):
    """Verify that video segments are being created with valid content"""
    try:
        hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
        segments = list(Path(hls_directory).glob("segment*.ts"))
        
        if not segments:
            logger.error(f"No segments found in {hls_directory}")
            return False
            
        # Check the size of the latest segment
        latest_segment = max(segments, key=lambda p: p.stat().st_mtime)
        size = latest_segment.stat().st_size
        
        if size < 1000:  # Less than 1KB is probably invalid
            logger.error(f"Latest segment {latest_segment} is too small: {size} bytes")
            return False
            
        logger.info(f"Latest segment {latest_segment} size: {size} bytes")
        return True
        
    except Exception as e:
        logger.error(f"Error verifying segments: {e}")
        return False

def get_camera_format(camera_id):
    """Get supported formats for the camera"""
    try:
        result = subprocess.run(
            ['v4l2-ctl', '--device', f'/dev/video{camera_id}', '--list-formats'],
            capture_output=True,
            text=True
        )
        logger.info(f"Camera {camera_id} formats: {result.stdout}")
        return "mjpeg" if "MJPG" in result.stdout else "yuyv422"
    except:
        return "yuyv422"  # Default to YUYV if can't determine

###############################################################################
# API endpoints
###############################################################################

@app.route('/api/start-streams', methods=['POST'])
def start_streams():
    """Start camera streams"""
    started = []
    errors = {}
    
    try:
        data = request.get_json()
        cameras = data.get("cameras", [])
        logger.info(f"Attempting to start cameras: {cameras}")
        
        if not cameras:
            return jsonify({"error": "No cameras specified"}), 400
            
        with process_lock:
            for camera_id in cameras:
                try:
                    camera_id = str(camera_id)
                    logger.info(f"Starting camera {camera_id}")
                    
                    # Stop any existing streams
                    if camera_id in active_ffmpeg_processes:
                        stop_streams([camera_id])
                    
                    # Force cleanup
                    cleanup_hls_files(camera_id)
                    release_camera(camera_id)
                    
                    # Test camera access
                    cap = cv2.VideoCapture(int(camera_id))
                    if not cap.isOpened():
                        raise Exception(f"Failed to open camera {camera_id}")
                    cap.release()
                    
                    # Start FFmpeg process
                    cmd = build_ffmpeg_command(camera_id, {})
                    logger.info(f"Starting FFmpeg: {' '.join(cmd)}")
                    
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        universal_newlines=True
                    )
                    
                    # Monitor FFmpeg output
                    monitor_ffmpeg_output(process, camera_id)
                    
                    # Wait for initial segments
                    time.sleep(3)
                    
                    # Check if process is still running
                    if process.poll() is not None:
                        raise Exception("FFmpeg process failed to start")
                    
                    # Check for segments
                    hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
                    segments = list(Path(hls_directory).glob("segment*.ts"))
                    if not segments:
                        raise Exception("No video segments created")
                    
                    active_ffmpeg_processes[camera_id] = {"main": process}
                    started.append(camera_id)
                    logger.info(f"Successfully started camera {camera_id}")
                    
                except Exception as e:
                    error_msg = f"Error starting camera {camera_id}: {str(e)}"
                    logger.error(error_msg)
                    errors[camera_id] = error_msg
                    
                    # Cleanup on error
                    if camera_id in active_ffmpeg_processes:
                        stop_streams([camera_id])
        
        return jsonify({
            "status": "success" if started else "error",
            "started": started,
            "errors": errors
        })
        
    except Exception as e:
        logger.error(f"Error in start_streams: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/stop-streams', methods=['POST'])
def stop_streams():
    """Stop all active FFmpeg processes and cleanup"""
    stopped = []
    errors = {}

    with process_lock:
        for camera_id, procs in list(active_ffmpeg_processes.items()):
            try:
                # Stop processes
                for proc_type, proc in procs.items():
                    if proc:
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                
                # Force release camera
                release_camera(camera_id)
                
                # Cleanup HLS files
                cleanup_hls_files(camera_id)
                
                stopped.append(camera_id)
                del active_ffmpeg_processes[camera_id]
                
            except Exception as e:
                logger.error(f"Error stopping stream for camera {camera_id}: {e}")
                errors[camera_id] = str(e)

    return jsonify({
        "status": "success" if not errors else "partial_success",
        "stopped": stopped,
        "errors": errors
    })

@app.route('/api/status', methods=['GET'])
def status():
    """
    Return the status of active streams. For each camera, indicate whether the main FFmpeg
    process is running.
    """
    status_info = {}
    with process_lock:
        for camera_id, procs in active_ffmpeg_processes.items():
            main_running = procs.get("main") and (procs.get("main").poll() is None)
            status_info[camera_id] = {"main": main_running}
    return jsonify({"active_streams": status_info}), 200

# Add static file serving for HLS and recordings
@app.route('/hls/<path:filename>')
def serve_hls(filename):
    return send_from_directory("hls", filename)

@app.route('/recordings/<path:filename>')
def serve_recording(filename):
    return send_from_directory(RECORDINGS_DIR, filename)

@app.route('/')
def admin_interface():
    return send_file('static/admin.html')

@app.route('/api/recordings')
def list_recordings():
    files = []
    try:
        files = os.listdir(RECORDINGS_DIR)
    except Exception as e:
        logger.error(f"Error listing recordings: {str(e)}")
    return jsonify({"recordings": files})

@app.route('/api/debug/camera/<camera_id>', methods=['GET'])
def debug_camera(camera_id):
    """Debug endpoint to check camera status"""
    try:
        cap = cv2.VideoCapture(int(camera_id))
        if not cap.isOpened():
            return jsonify({"error": f"Could not open camera {camera_id}"}), 400
        
        # Get camera properties
        props = {
            "width": cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            "height": cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            "fps": cap.get(cv2.CAP_PROP_FPS),
            "format": cap.get(cv2.CAP_PROP_FORMAT),
            "backend": cap.get(cv2.CAP_PROP_BACKEND),
        }
        
        ret, _ = cap.read()
        cap.release()
        
        if not ret:
            return jsonify({"error": "Could not read frame", "properties": props}), 400
            
        return jsonify({
            "status": "Camera accessible",
            "properties": props
        })
    except Exception as e:
        return jsonify({"error": f"Camera test failed: {str(e)}"}), 500

@app.route('/static/<path:path>')
def serve_static(path):
    """Serve static files including HLS streams"""
    return send_from_directory('static', path)

@app.route('/api/check-stream/<camera_id>')
def check_stream(camera_id):
    """Check if stream is working properly"""
    try:
        hls_directory = os.path.join("static", "hls", f"camera_{camera_id}")
        playlist_path = os.path.join(hls_directory, "playlist.m3u8")
        
        if not os.path.exists(playlist_path):
            return jsonify({"error": "Playlist not found"}), 404
            
        # Read playlist
        with open(playlist_path, 'r') as f:
            playlist = f.read()
            
        # Get segment info
        segments = list(Path(hls_directory).glob("segment*.ts"))
        segment_info = [{
            "name": s.name,
            "size": s.stat().st_size,
            "mtime": s.stat().st_mtime
        } for s in segments]
        
        # Check FFmpeg process
        process_info = None
        with process_lock:
            if camera_id in active_ffmpeg_processes:
                process = active_ffmpeg_processes[camera_id].get("main")
                if process:
                    process_info = {
                        "pid": process.pid,
                        "returncode": process.poll()
                    }
        
        return jsonify({
            "status": "ok",
            "playlist": playlist,
            "segments": segment_info,
            "process": process_info
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# Add error handler
@app.errorhandler(404)
def not_found(e):
    return jsonify(error=str(e)), 404

###############################################################################
# Run the Flask application
###############################################################################

def ensure_directories():
    """Ensure all required directories exist"""
    dirs = [
        "static/hls",
        "recordings",
        "logs"
    ]
    for d in dirs:
        Path(d).mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o755)

# Call this when the app starts
ensure_directories()

if __name__ == '__main__':
    # Create static and recordings directories if they don't exist
    os.makedirs('static', exist_ok=True)
    os.makedirs(RECORDINGS_DIR, exist_ok=True)
    app.run(host='0.0.0.0', port=5000, debug=False)
