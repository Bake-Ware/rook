package systems.bake.baketris;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.util.ArrayList;

/** Drives Baketris's own Game.java (copied, untouched, from /home/bake/baketris) through a
 *  script and prints the state after every command, as JSON lines: what the Python port
 *  (deck-remote/rookmote/games/rules.py) must match. tests/make_baketris_fixtures.py runs it.
 *  Commands: reset MODE SEED | L | R | cw | ccw | 180 | soft | hard | hold | u DT SOFT SDF |
 *  set ROW COL V | put TYPE ROT X Y | clear | grav */
final class Harness {
    static final ArrayList<String> events = new ArrayList<>();

    public static void main(String[] a) throws Exception {
        Game g = new Game();
        g.listener = new Game.Listener() {
            public void onMove() { events.add("\"move\""); }
            public void onRotate() { events.add("\"rotate\""); }
            public void onHold() { events.add("\"hold\""); }
            public void onHardDrop(int type, int rot, int x, int fromY, int toY) {
                events.add("[\"hard\"," + type + "," + rot + "," + x + "," + fromY + "," + toY + "]");
            }
            public void onLock(int cleared, int[] rows, int spin, boolean b2bBonus, int combo, boolean pc, boolean hard) {
                StringBuilder r = new StringBuilder("[");
                for (int i = 0; i < rows.length; i++) r.append(i > 0 ? "," : "").append(rows[i]);
                events.add("[\"lock\"," + cleared + "," + r + "]," + spin + "," + b2bBonus + "," + combo + "," + pc + "," + hard + "]");
            }
            public void onEnd(boolean win) { events.add("[\"end\"," + win + "]"); }
        };
        BufferedReader in = new BufferedReader(new InputStreamReader(System.in));
        String line;
        while ((line = in.readLine()) != null) {
            String[] p = line.trim().split(" ");
            if (p[0].isEmpty()) continue;
            events.clear();
            Object ret = null;
            switch (p[0]) {
                case "reset": g.reset(Integer.parseInt(p[1]), Long.parseLong(p[2])); g.nextPiece(); break;
                case "L": ret = g.shift(-1); break;
                case "R": ret = g.shift(1); break;
                case "cw": ret = g.rotate(1); break;
                case "ccw": ret = g.rotate(-1); break;
                case "180": ret = g.rotate(2); break;
                case "soft": ret = g.softStep(); break;
                case "hard": g.hardDrop(); break;
                case "hold": g.doHold(); break;
                case "u": g.update(Double.parseDouble(p[1]), p[2].equals("1"), Integer.parseInt(p[3])); break;
                case "set": g.grid[Integer.parseInt(p[1])][Integer.parseInt(p[2])] = Integer.parseInt(p[3]); break;
                case "clear": for (int[] row : g.grid) java.util.Arrays.fill(row, 0); break;
                case "put": g.cur = Integer.parseInt(p[1]); g.rot = Integer.parseInt(p[2]); g.px = Integer.parseInt(p[3]);
                            g.py = Integer.parseInt(p[4]); g.lastRotate = false; g.lastKick = -1; g.lockTimer = 0; g.resets = 0;
                            g.lowestY = g.py; break;
                case "grav": StringBuilder gs = new StringBuilder("[");
                             for (int lv = 1; lv <= 22; lv++) { g.level = lv; gs.append(lv > 1 ? "," : "").append(Double.toString(g.gravityMs())); }
                             System.out.println("{\"grav\":" + gs + "]}"); continue;
                default: throw new RuntimeException("bad command " + line);
            }
            StringBuilder s = new StringBuilder();
            s.append("{\"ret\":").append(ret == null ? "null" : ret.toString());
            s.append(",\"cur\":").append(g.cur).append(",\"rot\":").append(g.rot).append(",\"px\":").append(g.px).append(",\"py\":").append(g.py);
            s.append(",\"hold\":").append(g.hold).append(",\"holdUsed\":").append(g.holdUsed);
            s.append(",\"queue\":").append(g.queue.toString().replace(" ", ""));
            s.append(",\"score\":").append(g.score).append(",\"lines\":").append(g.lines).append(",\"level\":").append(g.level);
            s.append(",\"combo\":").append(g.combo).append(",\"b2b\":").append(g.b2b).append(",\"pieces\":").append(g.pieces);
            s.append(",\"over\":").append(g.over).append(",\"won\":").append(g.won).append(",\"lastKick\":").append(g.lastKick);
            s.append(",\"resets\":").append(g.resets).append(",\"lockTimer\":").append(Double.toString(g.lockTimer));
            s.append(",\"timeMs\":").append(Double.toString(g.timeMs));
            StringBuilder grid = new StringBuilder();
            for (int r = 0; r < 24; r++) for (int c = 0; c < Game.W; c++) grid.append(g.grid[r][c]);
            s.append(",\"grid\":\"").append(grid).append("\"");
            s.append(",\"events\":[").append(String.join(",", events)).append("]}");
            System.out.println(s);
        }
    }
}
