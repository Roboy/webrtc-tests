import argparse
import asyncio
import datetime
import json
import logging
import os
import ssl
import uuid

logger = logging.getLogger("pc")
try:
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ModuleNotFoundError as e:
    logger.warning("Could not find uvloop; installing it is recommended for performance improvements")

import aiortc.codecs.h264
# import cv2
from aiohttp import web
from av import VideoFrame

from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, clock
from aiortc.contrib.media import MediaBlackhole, MediaPlayer, MediaRecorder, MediaRelay

ROOT = os.path.dirname(__file__)

pcs = set()
relay = MediaRelay()
play_file = None

# Override bitrate parameters of h264
aiortc.codecs.h264.MIN_BITRATE = 100_000
aiortc.codecs.h264.MAX_BITRATE = 5_000_000


async def index(request):
    content = open(os.path.join(ROOT, "index.html"), "r").read()
    return web.Response(content_type="text/html", text=content)


async def javascript(request):
    content = open(os.path.join(ROOT, "client.js"), "r").read()
    return web.Response(content_type="application/javascript", text=content)


async def offer(request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])

    pc = RTCPeerConnection()
    pc_id = "PeerConnection(%s)" % uuid.uuid4()
    pcs.add(pc)

    def log_info(msg, *args):
        logger.info(pc_id + " " + msg, *args)

    log_info("Created for %s", request.remote)

    # prepare local media
    player = None if play_file is None else MediaPlayer(play_file, loop=True) # os.path.join(ROOT, "demo-instruct.wav"))
    if args.record_to:
        recorder = None # MediaRecorder(args.record_to)
    else:
        recorder = MediaBlackhole()

    video_sender = None

    @pc.on("datachannel")
    def on_datachannel(channel):

        stats_last_timestamp: datetime.datetime = clock.current_datetime()
        stats_last_bytecount = 0
        async def sendStats():
            nonlocal stats_last_bytecount, stats_last_timestamp
            stats = await video_sender.getStats()
            sender_stats = stats["outbound-rtp_" + str(id(video_sender))]
            current_timestamp: datetime.datetime = sender_stats.timestamp
            current_bytecount = sender_stats.bytesSent
            current_bps = (current_bytecount - stats_last_bytecount) / (current_timestamp - stats_last_timestamp).seconds
            encoder_name = str(video_sender._RTCRtpSender__encoder.__class__.__name__)
            if encoder_name == 'H264Encoder':
                encoder_name += ' / ' + str(video_sender._RTCRtpSender__encoder.codec.name)
            channel.send("stats " + json.dumps({
                "Codec": encoder_name,
                "Est. Bandwidth": video_sender.lastBitrateEstimate / 1000 if hasattr(video_sender, "lastBitrateEstimate") else 'n/a',
                "...Target kBit": video_sender._RTCRtpSender__encoder.target_bitrate / 1000,
                "..Current kBit": current_bps*8 / 1000
            }))
            stats_last_timestamp = current_timestamp
            stats_last_bytecount = current_bytecount

        @channel.on("message")
        async def on_message(message):
            if isinstance(message, str) and message.startswith("ping"):
                channel.send("pong" + message[4:])
                #stat = await video_sender.getStats()
                try:
                    #channel.send("vcodec is " + str(video_sender._RTCRtpSender__encoder or "") + " / " + str(video_sender._RTCRtpSender__encoder.codec or ""))
                    await sendStats()
                    pass
                except Exception as e:
                    logging.error(e)
            if isinstance(message, str) and message.startswith("target_bitrate"):
                try:
                    bitrate_target = int(message[14:])
                    video_sender._RTCRtpSender__encoder.target_bitrate = bitrate_target
                    channel.send("new bitrate target is " + str(bitrate_target) + " / " + str(video_sender._RTCRtpSender__encoder.target_bitrate))
                except Exception as e:
                    logging.error(e)

    @pc.on("connectionstatechange")
    async def on_connectionstatechange():
        log_info("Connection state is %s", pc.connectionState)

        if pc.connectionState == "connected":
            pass

        if pc.connectionState == "failed":
            await pc.close()
            pcs.discard(pc)

    @pc.on("track")
    def on_track(track):
        log_info("Track %s received", track.kind)

        if track.kind == "audio":
            # pc.addTrack(player.audio)
            if recorder is not None:
                recorder.addTrack(track)
        elif track.kind == "video":
            # pc.addTrack(
            #     VideoTransformTrack(
            #         relay.subscribe(track), transform=params["video_transform"]
            #     )
            # )
            if args.record_to and recorder is not None:
                recorder.addTrack(relay.subscribe(track))

        @track.on("ended")
        async def on_ended():
            log_info("Track %s ended", track.kind)
            if recorder is not None:
                await recorder.stop()

    # handle offer
    await pc.setRemoteDescription(offer)
    if recorder is not None:
        await recorder.start()

    if player and player.audio:
        pc.addTrack(player.audio)

    if player and player.video:
        video_sender = pc.addTrack(player.video)

    # send answer
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    return web.Response(
        content_type="application/json",
        text=json.dumps(
            {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
        ),
    )


async def on_shutdown(app):
    # close peer connections
    coros = [pc.close() for pc in pcs]
    await asyncio.gather(*coros)
    pcs.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="WebRTC audio / video / data-channels demo"
    )
    parser.add_argument("--cert-file", help="SSL certificate file (for HTTPS)")
    parser.add_argument("--key-file", help="SSL key file (for HTTPS)")
    parser.add_argument(
        "--host", default="0.0.0.0", help="Host for HTTP server (default: 0.0.0.0)"
    )
    parser.add_argument(
        "--port", type=int, default=8080, help="Port for HTTP server (default: 8080)"
    )
    parser.add_argument("--record-to", help="Write received media to a file."),
    parser.add_argument("--play-from", help="Read the media from a file and sent it."),
    parser.add_argument("--verbose", "-v", action="count")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    # create media source
    if args.play_from:
        play_file = args.play_from
        #logger.info("Playing %s", args.play_from)
    else:
        play_file = None

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_get("/", index)
    app.router.add_get("/client.js", javascript)
    app.router.add_post("/offer", offer)
    web.run_app(
        app, access_log=None, host=args.host, port=args.port, ssl_context=ssl_context
    )
