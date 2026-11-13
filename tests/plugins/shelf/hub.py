"""The sample plugin `shelf`: a page in its own segment, a settings section and a
component on the site (PL1, P1, P2), and cap `shelf.books` for the page to call."""

BOOKS = [{"title": "The Hobbit", "author": "J. R. R. Tolkien"},
         {"title": "Middlemarch", "author": "George Eliot"}]


def setup(plugin):
    @plugin.cap("shelf.books", "List the books on the shelf.", {
        "type": "object", "additionalProperties": False, "properties": {}})
    def books(call, args):
        return {"books": BOOKS}
