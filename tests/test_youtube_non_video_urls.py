"""A search, playlist or channel URL never reaches yt-dlp.

``noplaylist`` only strips the ``list=`` off a ``watch`` link; a
``results?search_query=`` or a bare ``/playlist`` is still walked entry by
entry. In the prod container on 17-sep-2026 one search URL held the probe
for 2220 s and failed on an entry that "premieres in 4 days", having paid
the per-GB proxy three times that day for the same nothing.
"""
import pytest

from yt_clients import youtube_non_video_reason


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=3QFAEqE9-Kk",
    "https://www.youtube.com/watch?v=3QFAEqE9-Kk&list=PLArw552Yzuwn&rco=1",
    "https://youtu.be/e6hD7QHib3I?si=GrijVOWTpP9BwZ7b",
    "https://m.youtube.com/watch?v=abc",
    "https://www.youtube.com/shorts/abc123",
    "https://www.youtube.com/live/abc123",
    "https://www.youtube.com/embed/abc123",
    "https://www.youtube.com/clip/UgkxAbc",
    "https://music.youtube.com/watch?v=abc",
    "https://vimeo.com/12345",
    "https://cdn.example.com/video.mp4",
])
def test_single_videos_and_foreign_hosts_pass(url):
    assert youtube_non_video_reason(url) is None


@pytest.mark.parametrize("url, word", [
    ("https://www.youtube.com/results?search_query=GAlatins+6", "search"),
    ("https://www.youtube.com/results", "search"),
    ("https://www.youtube.com/playlist?list=PLArw552Yzuwn", "playlist"),
    ("https://www.youtube.com/@openshorts", "channel"),
    ("https://www.youtube.com/channel/UCabc", "channel"),
    ("https://www.youtube.com/c/openshorts/videos", "channel"),
    ("https://www.youtube.com/user/openshorts", "channel"),
    ("https://www.youtube.com/feed/subscriptions", "no video"),
    ("https://www.youtube.com/", "no video"),
    ("https://www.youtube.com/watch", "without a video id"),
    ("https://youtu.be/", "without a video id"),
    # 20-sep-2026: a browse yt-dlp titled "viralshorts" walked to page 23 on
    # the static pool and then paid the per-GB proxy to walk it again. The
    # guard was a list of bad paths; these two are the shapes it let through.
    ("https://www.youtube.com/hashtag/viralshorts", "hashtag"),
    ("https://www.youtube.com/viralshorts", "not one video"),
    ("https://www.youtube.com/shorts", "without a video id"),
])
def test_pages_that_are_not_one_video_are_named(url, word):
    assert word in youtube_non_video_reason(url)


