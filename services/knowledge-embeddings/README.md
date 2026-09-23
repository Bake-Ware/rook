# Rook semantic embeddings

CPU-only FastEmbed using `sentence-transformers/all-MiniLM-L6-v2` (384 dimensions).
Model documentation: https://qdrant.github.io/fastembed/examples/Supported_Models/
Run on Soundwave's Docker container with a persistent `/models` volume, two CPU
threads, a 768 MiB limit, and only a loopback host port. A dedicated SSH reverse
forward exposes the endpoint on bakenetcanada's loopback, not the public network.
Set `ROOK_EMBED_URL=http://127.0.0.1:18768/embed` on the MCP service.
Initial image/model download needs internet; subsequent inference is local.
The API validates batch/text sizes and serializes inference. Knowledge retrieval
falls back to lexical search if this endpoint is unavailable. Index revisions and
model identity are stored with vectors; rebuilding does not alter source records.
