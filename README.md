---
title: HDTodayz Video Downloader
emoji: 🎬
colorFrom: red
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# HDTodayz Automated Video Downloader & Stream Engine

An automated streaming and direct-to-browser downloader for HDTodayz with zero server-side disk storage requirements.

## Features
- **Direct-to-Device Downloads:** Streams remuxed fragmented MP4 directly to the requesting browser (Content-Disposition: attachment).
- **Simultaneous Multi-Episode Downloads:** Concurrently download multiple episodes in Chrome via isolated iframe streams.
- **HLS Stream Acceleration:** Concurrent chunk prefetching in RAM with 3.5s fast-failover connection pool and zero stalls.
- **In-Browser HTML5 Player:** Watch movies and TV shows directly with instant seeking and embedded playback.
- **Full Metadata & Subtitles:** Automated TMDB integration, poster artwork, season/episode indexing, and WebVTT/SRT subtitle extraction.

## Deployment on Hugging Face Spaces
This repository is configured out-of-the-box for **Hugging Face Spaces** using the Docker SDK:
- **Port:** 7860
- **Engine:** Python 3.11 + FFmpeg
- **Container User:** Non-root UID 1000 (user)
