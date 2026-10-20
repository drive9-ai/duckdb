#!/bin/sh
set -eu
cd "$(dirname "$0")"
node render.cjs
ffmpeg -hide_banner -loglevel warning -y -i handoff-tuning.mp4 \
  -filter_complex '[0:v]fps=12,scale=1280:720:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff:max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle' \
  -loop 0 handoff-tuning.gif
ffprobe -v error -show_entries format=duration,size -show_entries stream=width,height,avg_frame_rate \
  -of json handoff-tuning.mp4 > qa/media.json
