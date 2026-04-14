# =============================================================================
# sources.py — The only file you need to edit to add or remove sources.
#
# To add a new source, append one dict to SOURCES:
#
#   {
#       "name": "Display name shown in the dashboard",
#       "url":  "Top-level URL to scrape",
#   }
#
# Pagination is handled automatically — no need to list individual page URLs.
# If a site has unusual markup, a custom parser can be added in scrape.py.
# =============================================================================

SOURCES = [
    {
        "name": "Picru",
        "url":  "https://picru.jp/opens/",
    },
    {
        "name": "フォトセカイ",
        "url":  "https://photosekai.com/post/photocontestlist/",
    },
    {
        "name": "山と渓谷",
        "url":  "https://www.yamakei-online.com/yk/pt_contest/",
    },
    {
        "name": "登竜門",
        "url":  "https://compe.japandesign.ne.jp/category/photo/",
    },

    # Add new sources below ↓
    # { "name": "GANREF", "url": "https://ganref.jp/photo_contests/jpn/" },
]