import pytest

from group_bot.links import canonical_key, extract_urls, is_shortener

SAME = [
    # Instagram: app share vs browser vs username prefix vs mirror
    ("https://www.instagram.com/reel/C8xYz12AbCd/?igsh=MWQ1ZGUxMzBkMA==",
     "https://instagram.com/reel/C8xYz12AbCd",
     "https://www.instagram.com/reels/C8xYz12AbCd/",
     "https://www.instagram.com/someuser/reel/C8xYz12AbCd/?utm_source=ig_web_copy_link",
     "https://ddinstagram.com/reel/C8xYz12AbCd/"),
    ("https://www.instagram.com/p/ABC123/?img_index=1&igsh=xyz",
     "https://instagram.com/p/ABC123"),
    # X / Twitter
    ("https://x.com/elonmusk/status/1790000000000000000?s=46&t=AbCdEf",
     "https://twitter.com/elonmusk/status/1790000000000000000",
     "https://mobile.twitter.com/someoneelse/status/1790000000000000000/photo/1",
     "https://fxtwitter.com/elonmusk/status/1790000000000000000",
     "https://x.com/i/web/status/1790000000000000000"),
    # TikTok
    ("https://www.tiktok.com/@user/video/7300000000000000000?is_from_webapp=1&sender_device=pc",
     "https://m.tiktok.com/@user/video/7300000000000000000"),
    # YouTube
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=abc",
     "https://youtu.be/dQw4w9WgXcQ?si=xyz",
     "https://m.youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
     "https://youtube.com/shorts/dQw4w9WgXcQ?feature=share"),
    # Reddit
    ("https://www.reddit.com/r/pics/comments/abc123/some_title/?utm_source=share",
     "https://old.reddit.com/r/pics/comments/abc123/",
     "https://redd.it/abc123"),
    # Generic site: tracking params and trailing slash
    ("https://example.com/news/story?utm_source=x&fbclid=123",
     "http://www.example.com/news/story/"),
]

DIFFERENT = [
    ("https://x.com/a/status/1", "https://x.com/a/status/2"),
    ("https://www.instagram.com/p/AAA/", "https://www.instagram.com/p/BBB/"),
    ("https://youtube.com/watch?v=aaa", "https://youtube.com/watch?v=bbb"),
    ("https://example.com/?page=1", "https://example.com/?page=2"),
    ("https://example.com/?s=cats", "https://example.com/?s=dogs"),
    ("https://www.reddit.com/r/a/comments/abc/t/", "https://www.reddit.com/r/a/comments/abc/t/xyz/"),
]


@pytest.mark.parametrize("group", SAME)
def test_same_post_same_key(group):
    keys = {canonical_key(u) for u in group}
    assert len(keys) == 1, keys


@pytest.mark.parametrize("a,b", DIFFERENT)
def test_different_posts_differ(a, b):
    assert canonical_key(a) != canonical_key(b)


def test_extract_urls():
    text = ("look at this https://x.com/a/status/1?s=20, and (https://youtu.be/abc). "
            "also www.example.com/page!")
    assert extract_urls(text) == [
        "https://x.com/a/status/1?s=20",
        "https://youtu.be/abc",
        "https://www.example.com/page",
    ]
    assert extract_urls(None) == []
    assert extract_urls("no links here") == []


def test_shorteners():
    assert is_shortener("https://vm.tiktok.com/ZMabc123/")
    assert is_shortener("https://t.co/xyz")
    assert is_shortener("https://www.reddit.com/r/pics/s/AbCdEf")
    assert is_shortener("https://www.instagram.com/share/reel/BAabc")
    assert not is_shortener("https://x.com/a/status/1")
