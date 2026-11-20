# PlatformIO pre-build script: give the build a three-word moniker.
#
# The moniker is Rook's record-name scheme (rook/core/names.py on master:
# adjective-noun-noun, the two nouns different) over the same word lists
# (copied to scripts/name_words/). It is derived from the build's sources, not
# picked at random: SHA-256 over the environment's name and every file under
# src/, include/ and lib/ plus platformio.ini (paths and contents, in path
# order). The same sources build under the same name, whether or not they are
# committed yet; a change anywhere in them gives a new one.
#
# The firmware gets it as BUILD_MONIKER; after the link the moniker and the
# image's SHA-256 are printed and kept in <build dir>/moniker.txt (scripts/
# publish.sh prints it when it publishes the image).
#
#   python3 scripts/moniker.py [env]     prints the moniker for env (default bridge)
import hashlib
import os
import sys


def _words(here, name):
    with open(os.path.join(here, "name_words", name), encoding="ascii") as f:
        return [w.strip() for w in f if w.strip() and not w.startswith("#")]


def source_hash(project, env_name):
    h = hashlib.sha256()
    h.update(b"env\0" + env_name.encode() + b"\0")
    files = [os.path.join(project, "platformio.ini")]
    for top in ("src", "include", "lib"):
        for root, dirs, names in os.walk(os.path.join(project, top)):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            files += [os.path.join(root, n) for n in names if not n.startswith(".") and not n.endswith(".pyc")]
    for path in sorted(files, key=lambda p: os.path.relpath(p, project)):
        rel = os.path.relpath(path, project).replace(os.sep, "/")
        with open(path, "rb") as f:
            data = f.read()
        h.update(rel.encode() + b"\0" + str(len(data)).encode() + b"\0" + data)
    return h.digest()


def moniker(project, env_name):
    here = os.path.join(project, "scripts")
    adj, nouns = _words(here, "adjectives.txt"), _words(here, "nouns.txt")
    d = source_hash(project, env_name)
    a = int.from_bytes(d[0:4], "big") % len(adj)
    n1 = int.from_bytes(d[4:8], "big") % len(nouns)
    n2 = int.from_bytes(d[8:12], "big") % (len(nouns) - 1)
    if n2 >= n1:
        n2 += 1                                # the two nouns differ
    return f"{adj[a]}-{nouns[n1]}-{nouns[n2]}"


try:
    Import("env")  # noqa: F821  (PlatformIO's SCons)
except NameError:
    env = None

if env is not None:
    _project = env.subst("$PROJECT_DIR")
    _env_name = env.subst("$PIOENV")
    _name = moniker(_project, _env_name)
    env.Append(CPPDEFINES=[("BUILD_MONIKER", '\\"%s\\"' % _name)])
    print("build moniker (%s): %s" % (_env_name, _name))

    def _after(source, target, env):
        bin_path = str(target[0])
        with open(bin_path, "rb") as f:
            data = f.read()
        sha = hashlib.sha256(data).hexdigest()
        line = "%s %s %s %d bytes sha256 %s" % (_env_name, _name, os.path.basename(bin_path), len(data), sha)
        with open(os.path.join(env.subst("$BUILD_DIR"), "moniker.txt"), "w") as f:
            f.write(_name + "\n" + line + "\n")
        print("build: " + line)

    env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", _after)
elif __name__ == "__main__":
    print(moniker(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  sys.argv[1] if len(sys.argv) > 1 else "bridge"))
