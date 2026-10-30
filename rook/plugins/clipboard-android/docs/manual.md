# Phone clipboard

`clipboard.set` puts text on the clipboard of a phone running the Rook app;
`clipboard.get` reads it. Android 10 and later hide the clipboard from apps
that are not in front, so a read from the band usually comes back empty
(`text: null`, with a note) unless the Rook app is open.
