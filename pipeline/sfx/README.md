# Sound effects

This folder is empty on purpose: the repository doesn't ship sound effects because their licenses vary.

Add `.mp3` or `.wav` files you have the right to use, then call them by file name:

```
python split_layout.py clips/my_clip --channel example_streamer --sfx boom@6.2 --out clips/READY_TO_POST/my_clip.mp4
```

`boom@6.2` plays `sfx/boom.mp3` (or `.wav`) 6.2 seconds into the finished clip. Your audio files are ignored by git.
