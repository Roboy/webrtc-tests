// get DOM elements
var dataChannelLog = document.getElementById('data-channel'),
    iceConnectionLog = document.getElementById('ice-connection-state'),
    iceGatheringLog = document.getElementById('ice-gathering-state'),
    signalingLog = document.getElementById('signaling-state'),
    dataPing = document.getElementById('ping'),
    statLog = document.getElementById('transmission-status');

var bitrate_slider = document.getElementById('target_bitrate'),
    bitrate_slider_value = document.getElementById('target_bitrate_value'),
    fps_slider = document.getElementById('target_fps'),
    fps_slider_value = document.getElementById('target_fps_value'),
    res_slider = document.getElementById('target_height'),
    res_slider_value = document.getElementById('target_height_value');

const height_values = [144, 240, 360, 480, 720, 1080];

bitrate_slider.value = 1000
bitrate_slider_value.innerText = bitrate_slider.value;
fps_slider.value = 30
fps_slider_value.innerText = fps_slider.value;
res_slider.min = 0;
res_slider.max = height_values.length-1;
res_slider.value = height_values.length-1;
res_slider_value.innerText = ""+height_values[res_slider.value];

// peer connection
var pc = null;

// data channel
var dc = null, dcInterval = null;

updateBitrate = function() {
    bitrate_slider_value.innerText = bitrate_slider.value;
    if(dc){
        dc.send('target_bitrate ' + bitrate_slider.value + '000');
    }
}
bitrate_slider.oninput = updateBitrate;
updateFPS = function() {
    fps_slider_value.innerText = fps_slider.value;
    if(dc){
        dc.send('target_fps ' + fps_slider.value);
    }
}
fps_slider.oninput = updateFPS;
updateRes = function() {
    res_slider_value.innerText = ""+height_values[res_slider.value];
    if(dc){
        dc.send('target_height ' + height_values[res_slider.value]);
    }
}
res_slider.oninput = updateRes;

function appendDataChannelLog(line){
    var scrolled = false;
    if(Math.abs(dataChannelLog.scrollHeight - dataChannelLog.clientHeight - dataChannelLog.scrollTop) < 10)
        scrolled = true;
    dataChannelLog.textContent += line+'\n';
    if(scrolled)
        dataChannelLog.scrollTop = dataChannelLog.scrollHeight;
}


function createPeerConnection() {
    var config = {
        sdpSemantics: 'unified-plan',
        bundlePolicy: 'max-bundle'
    };

    if (document.getElementById('use-stun').checked) {
        config.iceServers = [{urls: ['stun:stun.l.google.com:19302']}];
    }

    pc = new RTCPeerConnection(config);

    // register some listeners to help debugging
    pc.addEventListener('icegatheringstatechange', function() {
        iceGatheringLog.textContent += ' -> ' + pc.iceGatheringState;
    }, false);
    iceGatheringLog.textContent = pc.iceGatheringState;

    pc.addEventListener('iceconnectionstatechange', function() {
        iceConnectionLog.textContent += ' -> ' + pc.iceConnectionState;
    }, false);
    iceConnectionLog.textContent = pc.iceConnectionState;

    pc.addEventListener('signalingstatechange', function() {
        signalingLog.textContent += ' -> ' + pc.signalingState;
    }, false);
    signalingLog.textContent = pc.signalingState;

    // connect audio / video
    pc.addEventListener('track', function(evt) {
        console.log('received track', evt.track.kind, evt.track);
        if (evt.track.kind == 'video')
            document.getElementById('video').srcObject = evt.streams[0];
        else
            document.getElementById('audio').srcObject = evt.streams[0];
    });

    return pc;
}

function negotiate() {
    return pc.createOffer(
            {
                iceRestart: true,
                offerToReceiveVideo: true,
                offerToReceiveAudio: true
            })
        .then(function(offer) {
        return pc.setLocalDescription(offer);
    }).then(function() {
        // wait for ICE gathering to complete
        return new Promise(function(resolve) {
            if (pc.iceGatheringState === 'complete') {
                resolve();
            } else {
                function checkState() {
                    if (pc.iceGatheringState === 'complete') {
                        pc.removeEventListener('icegatheringstatechange', checkState);
                        resolve();
                    }
                }
                pc.addEventListener('icegatheringstatechange', checkState);
            }
        });
    }).then(function() {
        var offer = pc.localDescription;
        var codec;

        codec = document.getElementById('audio-codec').value;
        if (codec !== 'default') {
            offer.sdp = sdpFilterCodec('audio', codec, offer.sdp);
        }

        codec = document.getElementById('video-codec').value;
        if (codec !== 'default') {
            offer.sdp = sdpFilterCodec('video', codec, offer.sdp);
        }

        document.getElementById('offer-sdp').textContent = offer.sdp;
        return fetch('/offer', {
            body: JSON.stringify({
                sdp: offer.sdp,
                type: offer.type,
                //video_transform: document.getElementById('video-transform').value
            }),
            headers: {
                'Content-Type': 'application/json'
            },
            method: 'POST'
        });
    }).then(function(response) {
        return response.json();
    }).then(function(answer) {
        document.getElementById('answer-sdp').textContent = answer.sdp;
        return pc.setRemoteDescription(answer);
    }).catch(function(e) {
        console.error(e);
        alert(e);
    });
}


function start() {
    document.getElementById('start').style.display = 'none';

    pc = createPeerConnection();

    var time_start = null;

    function current_stamp() {
        if (time_start === null) {
            time_start = new Date().getTime();
            return 0;
        } else {
            return new Date().getTime() - time_start;
        }
    }

    if (true){ //document.getElementById('use-datachannel').checked) {
        var parameters = JSON.parse(document.getElementById('datachannel-parameters').value);

        dc = pc.createDataChannel('chat', parameters);
        dc.onclose = function() {
            clearInterval(dcInterval);
            appendDataChannelLog('- close');
        };
        dc.onopen = function() {
            dataChannelLog.textContent += '- open\n';
            dcInterval = setInterval(function() {
                var message = 'ping ' + current_stamp();
                appendDataChannelLog('> ' + message);
                dc.send(message);
            }, 1000);
            // send the current input values
            updateBitrate();
            updateFPS();
            updateRes();
        };
        dc.onmessage = function(evt) {
            appendDataChannelLog('< ' + evt.data);

            if (evt.data.substring(0, 4) === 'pong') {
                var elapsed_ms = current_stamp() - parseInt(evt.data.substring(5), 10);
                appendDataChannelLog(' RTT ' + elapsed_ms + ' ms');
                dataPing.innerText = elapsed_ms + ' ms';
            }
            if (evt.data.substring(0, 5) === 'stats') {
                var stats = JSON.parse(evt.data.substring(6));
                var iHtml = "";
                Object.entries(stats).forEach(([k, v]) => {iHtml += k + ": " + v + "\n";})
                statLog.innerText = iHtml;
            }
        };
    }

    var constraints = {
        audio: true, //document.getElementById('use-audio').checked,
        video: false
    };

    if (true){ //document.getElementById('use-video').checked) {
        //var resolution = document.getElementById('video-resolution').value;
        if (false){ //resolution) {
            resolution = resolution.split('x');
            constraints.video = {
                width: parseInt(resolution[0], 0),
                height: parseInt(resolution[1], 0)
            };
        } else {
            constraints.video = true;
        }
    }

    document.getElementById('media').style.display = 'block';
    if (document.getElementById('use-media').checked){//constraints.audio || constraints.video) {
        navigator.mediaDevices.getUserMedia(constraints).then(function(stream) {
            stream.getTracks().forEach(function(track) {
                pc.addTrack(track, stream);
            });
            return negotiate();
        }, function(err) {
            alert('Could not acquire media: ' + err);
        });
    } else {
        // enabled offerToReceive* in createOffer instead
        //pc.addTransceiver('video', {direction: "recvonly", streams: [new MediaStream()]});
        //pc.addTransceiver('audio', {direction: "recvonly", streams: [new MediaStream()]});
        pc.restartIce();
        setTimeout(negotiate, 100);
    }

    document.getElementById('stop').style.display = 'inline-block';
}

function stop() {
    document.getElementById('stop').style.display = 'none';

    // close data channel
    if (dc) {
        dc.close();
    }

    // close transceivers
    if (pc.getTransceivers) {
        pc.getTransceivers().forEach(function(transceiver) {
            if (transceiver.stop) {
                transceiver.stop();
            }
        });
    }

    // close local audio / video
    pc.getSenders().forEach(function(sender) {
        sender.track.stop();
    });

    // close peer connection
    setTimeout(function() {
        pc.close();
    }, 500);
}

function sdpFilterCodec(kind, codec, realSdp) {
    var allowed = []
    var rtxRegex = new RegExp('a=fmtp:(\\d+) apt=(\\d+)\r$');
    var codecRegex = new RegExp('a=rtpmap:([0-9]+) ' + escapeRegExp(codec))
    var videoRegex = new RegExp('(m=' + kind + ' .*?)( ([0-9]+))*\\s*$')
    
    var lines = realSdp.split('\n');

    var isKind = false;
    for (var i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('m=' + kind + ' ')) {
            isKind = true;
        } else if (lines[i].startsWith('m=')) {
            isKind = false;
        }

        if (isKind) {
            var match = lines[i].match(codecRegex);
            if (match) {
                allowed.push(parseInt(match[1]));
            }

            match = lines[i].match(rtxRegex);
            if (match && allowed.includes(parseInt(match[2]))) {
                allowed.push(parseInt(match[1]));
            }
        }
    }

    var skipRegex = 'a=(fmtp|rtcp-fb|rtpmap):([0-9]+)';
    var sdp = '';

    isKind = false;
    for (var i = 0; i < lines.length; i++) {
        if (lines[i].startsWith('m=' + kind + ' ')) {
            isKind = true;
        } else if (lines[i].startsWith('m=')) {
            isKind = false;
        }

        if (isKind) {
            var skipMatch = lines[i].match(skipRegex);
            if (skipMatch && !allowed.includes(parseInt(skipMatch[2]))) {
                continue;
            } else if (lines[i].match(videoRegex)) {
                sdp += lines[i].replace(videoRegex, '$1 ' + allowed.join(' ')) + '\n';
            } else {
                sdp += lines[i] + '\n';
            }
        } else {
            sdp += lines[i] + '\n';
        }
    }

    return sdp;
}

function escapeRegExp(string) {
    return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); // $& means the whole matched string
}
