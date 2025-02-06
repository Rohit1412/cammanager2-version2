class CameraManager {
    constructor() {
        this.cameras = [];
        this.init();
    }

    async init() {
        this.setupEventListeners();
        this.loadRecordings();
        this.startStatusPolling();
        this.initialLoadCameras();
    }

    initialLoadCameras() {
        fetch('/api/status')
            .then(res => res.json())
            .then(({ active_streams }) => {
                this.cameras = Object.keys(active_streams);
                this.renderCameraGrid();
            });
    }

    setupEventListeners() {
        document.addEventListener('click', async (e) => {
            if (e.target.classList.contains('control-btn')) {
                const cameraId = e.target.dataset.camera;
                const action = e.target.dataset.action;
                await this.handleControlAction(cameraId, action);
            }
        });

        document.getElementById('add-camera-btn').addEventListener('click', () => {
            const cameraId = document.getElementById('new-camera-id').value.trim();
            if (cameraId && !this.cameras.includes(cameraId)) {
                this.createCameraCard(cameraId);
                document.getElementById('new-camera-id').value = '';
            }
        });
    }

    updateButtonStates(cameraId, isStarted) {
        const card = document.querySelector(`#camera-${cameraId}`);
        if (!card) return;

        const startBtn = card.querySelector('.start-btn');
        const stopBtn = card.querySelector('.stop-btn');

        if (startBtn) {
            startBtn.disabled = isStarted;
        }
        if (stopBtn) {
            stopBtn.disabled = !isStarted;
        }

        // Update status indicator if it exists
        const statusDot = card.querySelector('.status-indicator');
        if (statusDot) {
            statusDot.classList.toggle('active', isStarted);
        }
    }

    async handleControlAction(cameraId, action) {
        try {
            console.log(`Handling ${action} for camera ${cameraId}`);
            const endpoint = action === 'start' ? '/api/start-streams' : '/api/stop-streams';
            const cameras = [cameraId];
            
            console.log(`Sending request to ${endpoint} with cameras:`, cameras);
            const response = await fetch(endpoint, {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({ cameras })
            });
            
            const data = await response.json();
            console.log(`Response from ${endpoint}:`, data);
            
            if (!response.ok) {
                throw new Error(data.message || 'Failed to control camera');
            }
            
            // Update UI based on action
            if (action === 'start') {
                // Update button states
                this.updateButtonStates(cameraId, true);
                // Initialize video stream
                console.log(`Initializing video stream for camera ${cameraId}`);
                this.initVideoStream(cameraId);
            } else {
                // Update button states for stop
                this.updateButtonStates(cameraId, false);
                // Destroy video stream
                this.destroyVideoStream(cameraId);
            }
            
        } catch (error) {
            console.error(`Error in handleControlAction:`, error);
            alert(`Failed to ${action} camera ${cameraId}: ${error.message}`);
            // Reset button states on error
            this.updateButtonStates(cameraId, action !== 'start');
        }
    }

    updateCameraUI(cameraId, action) {
        const card = document.querySelector(`#camera-${cameraId}`);
        if (action === 'start') {
            card.querySelector('.stop-btn').disabled = false;
            this.initVideoStream(cameraId);
        } else {
            card.querySelector('.stop-btn').disabled = true;
            this.destroyVideoStream(cameraId);
        }
    }

    async loadRecordings() {
        try {
            const response = await fetch('/api/recordings');
            const {recordings} = await response.json();
            this.renderRecordings(recordings);
        } catch (error) {
            this.showNotification('Failed to load recordings', 'error');
        }
    }

    renderRecordings(recordings) {
        const container = document.getElementById('recordings-list');
        container.innerHTML = recordings.map(rec => `
            <div class="recording-item" data-file="${rec}">
                <div class="recording-name">${rec}</div>
                <button class="btn btn-primary" onclick="playRecording('${rec}')">Play</button>
            </div>
        `).join('');
    }

    showNotification(message, type = 'info') {
        const container = document.getElementById('notifications');
        const notification = document.createElement('div');
        notification.className = `notification ${type}`;
        notification.innerHTML = `
            <span>${message}</span>
            <button class="close-btn">&times;</button>
        `;
        
        notification.querySelector('.close-btn').onclick = () => 
            notification.remove();
        
        container.appendChild(notification);
        setTimeout(() => notification.remove(), 5000);
    }

    startStatusPolling() {
        setInterval(async () => {
            try {
                const response = await fetch('/api/status');
                const {active_streams} = await response.json();
                this.updateStatusDisplay(active_streams);
            } catch (error) {
                this.showNotification('Status update failed', 'error');
            }
        }, 3000);
    }

    updateStatusDisplay(status) {
        document.getElementById('active-cam-count').textContent = 
            Object.keys(status).length;

        this.cameras.forEach(camId => {
            const camStatus = status[camId];
            const indicator = document.querySelector(`#camera-${camId} .status-indicator`);
            if (indicator) {
                indicator.className = `status-dot ${camStatus?.main ? 'healthy' : 'error'}`;
            }
        });
    }

    initVideoStream(cameraId) {
        const videoContainer = document.querySelector(`#camera-${cameraId} .video-container`);
        if (!videoContainer) return;
        
        // Clear existing video
        videoContainer.innerHTML = '';
        
        // Create video element
        const video = document.createElement('video');
        video.className = 'video-feed';
        video.controls = true;
        video.muted = true;
        video.autoplay = true;
        video.playsInline = true;
        
        videoContainer.appendChild(video);
        
        // Initialize HLS
        if (Hls.isSupported()) {
            const hls = new Hls({
                debug: true,  // Enable debug logging
                enableWorker: true,
                lowLatencyMode: false,
                maxBufferLength: 10,
                maxMaxBufferLength: 20,
                manifestLoadingTimeOut: 10000,
                manifestLoadingMaxRetry: 3,
                levelLoadingTimeOut: 10000,
                levelLoadingMaxRetry: 3,
                fragLoadingTimeOut: 10000,
                fragLoadingMaxRetry: 3
            });

            const hlsUrl = `/static/hls/camera_${cameraId}/playlist.m3u8`;
            console.log(`Loading HLS stream from: ${hlsUrl}`);
            
            // Add event listeners before loading source
            hls.on(Hls.Events.ERROR, (event, data) => {
                console.error('HLS Error:', data);
                if (data.fatal) {
                    switch(data.type) {
                        case Hls.ErrorTypes.NETWORK_ERROR:
                            console.log('Network error, attempting recovery');
                            hls.startLoad();
                            break;
                        case Hls.ErrorTypes.MEDIA_ERROR:
                            console.log('Media error, attempting recovery');
                            hls.recoverMediaError();
                            break;
                        default:
                            console.log('Fatal error, destroying player');
                            hls.destroy();
                            break;
                    }
                }
            });

            // Load source and attach media
            hls.loadSource(hlsUrl);
            hls.attachMedia(video);
            
            hls.on(Hls.Events.MANIFEST_PARSED, () => {
                console.log('HLS Manifest parsed, attempting playback');
                video.play()
                    .then(() => console.log('Playback started'))
                    .catch(e => {
                        console.error('Playback failed:', e);
                        video.muted = true;
                        return video.play();
                    });
            });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
            // Fallback for Safari
            video.src = `/static/hls/camera_${cameraId}/playlist.m3u8`;
            video.addEventListener('loadedmetadata', () => {
                video.play()
                    .then(() => console.log('Native playback started'))
                    .catch(e => console.error('Native playback failed:', e));
            });
        }
    }

    destroyVideoStream(cameraId) {
        const videoContainer = document.querySelector(`#camera-${cameraId} .video-container`);
        if (videoContainer) {
            const video = videoContainer.querySelector('video');
            if (video) {
                if (video.hls) {
                    video.hls.destroy();
                }
                video.remove();
            }
        }
    }

    renderCameraGrid() {
        const grid = document.getElementById('camera-grid');
        grid.innerHTML = '';
        this.cameras.forEach(camId => this.createCameraCard(camId));
    }

    createCameraCard(cameraId) {
        if (!this.cameras.includes(cameraId)) {
            this.cameras.push(cameraId);
            this.renderCameraCard(cameraId);
        }
    }

    renderCameraCard(cameraId) {
        const grid = document.getElementById('camera-grid');
        const card = document.createElement('div');
        card.id = `camera-${cameraId}`;
        card.className = 'camera-card';
        card.innerHTML = `
            <h3>Camera ${cameraId}</h3>
            <div class="status-indicator"></div>
            <div class="video-container"></div>
            <div class="controls">
                <button class="btn btn-primary control-btn start-btn" 
                        data-camera="${cameraId}" data-action="start">
                    Start
                </button>
                <button class="btn btn-danger control-btn stop-btn" 
                        data-camera="${cameraId}" data-action="stop" disabled>
                    Stop
                </button>
            </div>
        `;
        grid.appendChild(card);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    window.cameraManager = new CameraManager();
}); 