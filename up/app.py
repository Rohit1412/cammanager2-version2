import os
import subprocess
import logging
from flask import Flask, jsonify, request, send_from_directory, send_file
from threading import Lock
from datetime import datetime
import threading
import cv2
from pathlib import Path
import time
import psutil
import math

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
    recording_path = None
    
    if outputs.get("recording", {}).get("enabled", True):
        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        recording_path = os.path.join(RECORDINGS_DIR, f"camera_{camera_id}_{timestamp}.mp4")

    command = [
        "ffmpeg",
        "-y",
        # Input options
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", "640x480",
        "-framerate", "30",
        "-thread_queue_size", "512",  # Reduced queue size
        "-probesize", "42M",
        "-analyzeduration", "10M",
        "-i", f"/dev/video{camera_id}",  # Add input device
    ]

    # First output - MP4 Recording
    if recording_path:
        command.extend([
            "-c:v", "libx264",
            "-crf", "28",  # Lower quality but less CPU
            "-preset", "superfast",  # Much faster encoding
            "-pix_fmt", "yuv420p",
            "-r", "15",  # Lower framerate for recording
            "-g", "60",
            "-f", "mp4",
            recording_path,
        ])

    # Second output - HLS Stream
    command.extend([
        "-c:v", "libx264",
        "-preset", "superfast",
        "-tune", "zerolatency",
        "-pix_fmt", "yuv420p",
        "-b:v", "800k",  # Lower bitrate
        "-bufsize", "400k",
        "-maxrate", "1000k",
        "-g", "15",
        "-keyint_min", "15",
        "-r", "15",  # Lower framerate
        "-f", "hls",
        "-hls_time", "2",  # Longer segments = less CPU
        "-hls_list_size", "3",
        "-hls_init_time", "2",
        "-hls_allow_cache", "0",
        "-hls_segment_type", "mpegts",
        "-start_number", "0",
        "-hls_flags",
        "delete_segments+append_list+omit_endlist+round_durations+temp_file",
        "-hls_segment_filename", f"{hls_directory}/segment%03d.ts",
        playlist_path
    ])

    return command

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
    """Monitor FFmpeg process output in a separate thread"""
    def _monitor():
        while True:
            line = process.stderr.readline()
            if not line:
                break
            line = line.strip()
            if line:
                logger.info(f"FFmpeg camera {camera_id}: {line}")
                if "error" in line.lower():
                    logger.error(f"FFmpeg error for camera {camera_id}: {line}")
                    # Kill the process if there's a critical error
                    if "baseline profile doesn't support" in line or "Invalid data" in line:
                        process.kill()
                        break

    thread = threading.Thread(target=_monitor, daemon=True)
    thread.start()
    return thread

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

def calculate_camera_capacity(cpu_count, available_memory_gb, cpu_usage, memory_usage):
    """Calculate estimated camera capacity based on system resources"""
    try:
        # CPU usage estimates per resolution (percentage per stream)
        cpu_usage_per_camera = {
            '1080p': 50,  # 50% CPU per stream (more realistic for high quality)
            '720p': 35,   # 35% CPU per stream
            '480p': 25,   # 25% CPU per stream
            '360p': 15    # 15% CPU per stream
        }
        
        # Memory usage estimates per resolution (GB per stream)
        memory_per_camera = {
            '1080p': 0.75,   # 750MB per stream
            '720p': 0.5,    # 500MB per stream
            '480p': 0.35,   # 350MB per stream
            '360p': 0.25    # 250MB per stream
        }
        
        # Calculate available resources (leave 30% headroom instead of 20%)
        available_cpu = max(0, (100 - cpu_usage - 30) * cpu_count)  # Leave 30% headroom
        available_memory = max(0, available_memory_gb * 0.7)  # Leave 30% memory free
        
        # Network bandwidth estimation (Mbps)
        bandwidth_per_camera = {
            '1080p': 10,   # 10 Mbps for 1080p
            '720p': 7,     # 7 Mbps for 720p
            '480p': 4,     # 4 Mbps for 480p
            '360p': 2      # 2 Mbps for 360p
        }
        
        # Assume 1Gbps network capacity with 50% utilization (more conservative)
        available_bandwidth = 1000 * 0.5  # 500 Mbps usable
        
        # Calculate maximum cameras for each resolution
        max_cameras = {}
        for resolution in cpu_usage_per_camera:
            cpu_limit = int(available_cpu / cpu_usage_per_camera[resolution])
            mem_limit = int(available_memory / memory_per_camera[resolution])
            bandwidth_limit = int(available_bandwidth / bandwidth_per_camera[resolution])
            
            # Use the lowest of all three limits
            max_cameras[resolution] = min(cpu_limit, mem_limit, bandwidth_limit)
            
            # Add more conservative maximum limits
            if resolution == '1080p':
                max_cameras[resolution] = min(max_cameras[resolution], 4)  # Max 4 1080p streams
            elif resolution == '720p':
                max_cameras[resolution] = min(max_cameras[resolution], 6)  # Max 6 720p streams
            elif resolution == '480p':
                max_cameras[resolution] = min(max_cameras[resolution], 8)  # Max 8 480p streams
            elif resolution == '360p':
                max_cameras[resolution] = min(max_cameras[resolution], 12)  # Max 12 360p streams
        
        return max_cameras
        
    except Exception as e:
        logger.error(f"Error calculating camera capacity: {e}")
        return {
            '1080p': 0,
            '720p': 0,
            '480p': 0,
            '360p': 0
        }

def get_system_resources():
    """Get system resources and estimated camera capacity"""
    try:
        # CPU Info - Add small delay for accurate reading
        psutil.cpu_percent(interval=None)  # First call to initialize
        cpu_percent_per_core = psutil.cpu_percent(interval=0.5, percpu=True)
        total_cpu_percent = sum(cpu_percent_per_core) / len(cpu_percent_per_core)
        cpu_count = psutil.cpu_count(logical=True)
        
        # Memory Info
        memory = psutil.virtual_memory()
        
        # Disk Info
        disk = psutil.disk_usage('/')
        
        # Calculate estimated camera capacity
        estimated_capacity = calculate_camera_capacity(
            cpu_count=cpu_count,
            available_memory_gb=memory.available / (1024**3),
            cpu_usage=total_cpu_percent,
            memory_usage=memory.percent
        )
        
        # Round floating point values for cleaner display
        memory_total = round(memory.total / (1024**3), 1)
        memory_available = round(memory.available / (1024**3), 1)
        disk_total = round(disk.total / (1024**3), 1)
        disk_free = round(disk.free / (1024**3), 1)
        disk_percent = round((disk.used / disk.total) * 100, 1)
        
        return {
            "cpu": {
                "percent_used": round(total_cpu_percent, 1),
                "per_core_usage": [round(x, 1) for x in cpu_percent_per_core],
                "core_count": cpu_count
            },
            "memory": {
                "total_gb": memory_total,
                "available_gb": memory_available,
                "percent_used": memory.percent
            },
            "disk": {
                "total_gb": disk_total,
                "free_gb": disk_free,
                "percent_used": disk_percent
            },
            "estimated_capacity": estimated_capacity
        }
    except Exception as e:
        logger.exception(f"Error getting system resources: {e}")
        return None

def set_cpu_affinity(process, camera_id):
    """Set CPU affinity for FFmpeg process to distribute load"""
    try:
        p = psutil.Process(process.pid)
        cpu_count = psutil.cpu_count()
        # Distribute processes across CPUs
        cpu_set = {(int(camera_id) * 2) % cpu_count, 
                  (int(camera_id) * 2 + 1) % cpu_count}
        p.cpu_affinity(list(cpu_set))
    except Exception as e:
        logger.warning(f"Could not set CPU affinity: {e}")

def set_process_priority(process):
    """Set FFmpeg process priority to below normal"""
    try:
        p = psutil.Process(process.pid)
        p.nice(10)  # Lower priority (higher number = lower priority)
    except Exception as e:
        logger.warning(f"Could not set process priority: {e}")

def check_system_load():
    """Check if system has enough resources for new stream"""
    try:
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        # Don't start new streams if system is too loaded
        if cpu_percent > 80:
            raise Exception("System CPU usage too high (>80%)")
        if memory.percent > 85:
            raise Exception("System memory usage too high (>85%)")
        return True
    except Exception as e:
        logger.error(f"Resource check failed: {e}")
        return False

###############################################################################
# API endpoints
###############################################################################

@app.route('/api/start-streams', methods=['POST'])
def start_streams():
    try:
        if not check_system_load():
            return jsonify({"error": "System resources too low"}), 503
        
        data = request.get_json()
        cameras = data.get("cameras", [])
        
        logger.info(f"Attempting to start cameras: {cameras}")
        
        for camera_id in cameras:
            try:
                logger.info(f"Starting camera {camera_id}")
                
                # Build FFmpeg command
                cmd = build_ffmpeg_command(camera_id, data.get("outputs", {}))
                logger.info(f"Starting FFmpeg: {' '.join(cmd)}")
                
                # Check if device exists
                if not os.path.exists(f"/dev/video{camera_id}"):
                    raise Exception(f"Camera device /dev/video{camera_id} not found")
                
                # Start FFmpeg process
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    universal_newlines=True,
                    bufsize=1  # Line buffered
                )
                
                # Wait a short time to check if process started successfully
                time.sleep(1)
                if process.poll() is not None:
                    # Process already terminated
                    out, err = process.communicate()
                    raise Exception(f"FFmpeg failed to start: {err}")
                
                # Store process reference
                with process_lock:
                    active_ffmpeg_processes[camera_id] = {
                        "main": process,
                        "start_time": time.time()
                    }
                
                # Set process priority and CPU affinity
                set_cpu_affinity(process, camera_id)
                set_process_priority(process)
                
            except Exception as e:
                logger.error(f"Error starting camera {camera_id}: {str(e)}")
                return jsonify({"error": f"Failed to start camera {camera_id}: {str(e)}"}), 500
        
        return jsonify({"status": "success"})
        
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

@app.route('/api/recordings')
def list_recordings():
    """List all recordings with metadata"""
    try:
        recordings = []
        for file in os.listdir(RECORDINGS_DIR):
            if file.endswith('.mp4'):
                file_path = os.path.join(RECORDINGS_DIR, file)
                stat = os.stat(file_path)
                recordings.append({
                    'filename': file,
                    'size': stat.st_size,
                    'created': stat.st_mtime,
                    'camera_id': file.split('_')[1],
                    'timestamp': file.split('_')[2].replace('.mp4', '')
                })
        return jsonify({
            'status': 'success',
            'recordings': sorted(recordings, key=lambda x: x['created'], reverse=True)
        })
    except Exception as e:
        logger.error(f"Error listing recordings: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/recordings/<recording>')
def serve_recording(recording):
    """Serve a recording file"""
    try:
        if '..' in recording or '/' in recording:
            return jsonify({'error': 'Invalid filename'}), 400
            
        file_path = os.path.join(RECORDINGS_DIR, recording)
        if not os.path.exists(file_path):
            return jsonify({'error': 'Recording not found'}), 404
            
        download = request.args.get('download', 'false').lower() == 'true'
        if download:
            return send_file(file_path, as_attachment=True)
        else:
            return send_file(file_path)
            
    except Exception as e:
        logger.error(f"Error serving recording: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/recordings/<recording>/delete', methods=['POST'])
def delete_recording(recording):
    """Delete a recording file"""
    try:
        if '..' in recording or '/' in recording:
            return jsonify({'error': 'Invalid filename'}), 400
            
        file_path = os.path.join(RECORDINGS_DIR, recording)
        if not os.path.exists(file_path):
            return jsonify({'error': 'Recording not found'}), 404
            
        # Delete thumbnail if it exists
        thumbnail_path = os.path.join(RECORDINGS_DIR, f"thumb_{recording}.jpg")
        if os.path.exists(thumbnail_path):
            os.remove(thumbnail_path)
            
        # Delete recording
        os.remove(file_path)
        return jsonify({'status': 'success'})
        
    except Exception as e:
        logger.error(f"Error deleting recording: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/')
def admin_interface():
    return send_file('static/admin.html')

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

@app.route('/api/system-resources', methods=['GET'])
def system_resources():
    """Get system resources and estimated camera capacity"""
    resources = get_system_resources()
    if resources is None:
        return jsonify({"error": "Failed to get system resources"}), 500
    return jsonify(resources)

@app.route('/recordings')
def recordings_page():
    return send_file('static/recordings.html')

@app.route('/api/recordings/<recording>/thumbnail')
def get_recording_thumbnail(recording):
    """Generate and return a thumbnail for the recording"""
    try:
        recording_path = os.path.join(RECORDINGS_DIR, recording)
        if not os.path.exists(recording_path):
            return jsonify({"error": "Recording not found"}), 404

        # Generate thumbnail using FFmpeg
        thumbnail_path = os.path.join(RECORDINGS_DIR, f"thumb_{recording}.jpg")
        if not os.path.exists(thumbnail_path):
            subprocess.run([
                'ffmpeg', '-i', recording_path,
                '-ss', '00:00:01',
                '-vframes', '1',
                '-vf', 'scale=300:-1',
                thumbnail_path
            ], check=True)

        return send_file(thumbnail_path, mimetype='image/jpeg')
    except Exception as e:
        logger.error(f"Error generating thumbnail: {e}")
        return jsonify({"error": "Failed to generate thumbnail"}), 500

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
