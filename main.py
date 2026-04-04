from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import Optional, List
import sqlite3, json, os

app = FastAPI(title="Torneo de Tenis")

DB = "torneo.db"

def get_db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS jugadores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nombre TEXT NOT NULL,
            seed INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS partidos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ronda INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            p1_id INTEGER,
            p2_id INTEGER,
            score1 TEXT DEFAULT '[]',
            score2 TEXT DEFAULT '[]',
            winner_id INTEGER,
            next_match_id INTEGER,
            next_slot INTEGER
        );
        CREATE TABLE IF NOT EXISTS config (
            clave TEXT PRIMARY KEY,
            valor TEXT NOT NULL
        );
        INSERT OR IGNORE INTO config VALUES ('puntos', '{"champion":1000,"finalist":600,"semi":360,"quarter":180,"first":90}');
    """)
    conn.commit()
    conn.close()

init_db()

# --- MODELOS ---
class Jugador(BaseModel):
    nombre: str
    seed: Optional[int] = None

class MatchResult(BaseModel):
    score1: List[int] = []
    score2: List[int] = []
    winner_id: Optional[int] = None

class PuntosConfig(BaseModel):
    champion: int = 1000
    finalist: int = 600
    semi: int = 360
    quarter: int = 180
    first: int = 90

# --- JUGADORES ---
@app.get("/api/jugadores")
def get_jugadores():
    conn = get_db()
    rows = conn.execute("SELECT * FROM jugadores ORDER BY seed").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/jugadores")
def add_jugador(j: Jugador):
    conn = get_db()
    seed = j.seed if j.seed else conn.execute("SELECT COUNT(*)+1 FROM jugadores").fetchone()[0]
    cur = conn.execute("INSERT INTO jugadores (nombre, seed) VALUES (?,?)", (j.nombre, seed))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return {"id": new_id, "nombre": j.nombre, "seed": seed}

@app.delete("/api/jugadores/{jugador_id}")
def delete_jugador(jugador_id: int):
    conn = get_db()
    conn.execute("DELETE FROM jugadores WHERE id=?", (jugador_id,))
    conn.commit()
    conn.close()
    return {"ok": True}

# --- PARTIDOS ---
@app.get("/api/partidos")
def get_partidos():
    conn = get_db()
    rows = conn.execute("SELECT * FROM partidos ORDER BY ronda, idx").fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d['score1'] = json.loads(d['score1'])
        d['score2'] = json.loads(d['score2'])
        result.append(d)
    return result

@app.post("/api/partidos/generar")
def generar_fixture():
    conn = get_db()
    conn.execute("DELETE FROM partidos")
    jugadores = [dict(r) for r in conn.execute("SELECT * FROM jugadores ORDER BY seed").fetchall()]
    n = len(jugadores)
    if n < 2:
        conn.close()
        raise HTTPException(400, "Se necesitan al menos 2 jugadores")

    import math
    size = 2 ** math.ceil(math.log2(n))
    slots = [None] * size

    # Sembrar jugadores en posiciones
    positions = [0, size-1, size//2, size//2-1]
    for i, p in enumerate(jugadores):
        if i < len(positions):
            slots[positions[i]] = p['id']
        else:
            for j in range(size):
                if slots[j] is None:
                    slots[j] = p['id']
                    break

    rounds = int(math.log2(size))
    match_id_map = {}  # (ronda, idx) -> db_id
    next_match_id_val = [1]

    all_matches = []
    for r in range(rounds):
        count = size // (2 ** (r+1))
        for i in range(count):
            p1 = slots[i*2] if r == 0 else None
            p2 = slots[i*2+1] if r == 0 else None
            all_matches.append({
                'ronda': r, 'idx': i,
                'p1_id': p1, 'p2_id': p2,
                'score1': '[]', 'score2': '[]',
                'winner_id': None,
                'next_match_id': None, 'next_slot': None
            })

    # Insertar todos primero
    ids = []
    for m in all_matches:
        cur = conn.execute(
            "INSERT INTO partidos (ronda,idx,p1_id,p2_id,score1,score2,winner_id,next_match_id,next_slot) VALUES (?,?,?,?,?,?,?,?,?)",
            (m['ronda'],m['idx'],m['p1_id'],m['p2_id'],m['score1'],m['score2'],m['winner_id'],m['next_match_id'],m['next_slot'])
        )
        ids.append(cur.lastrowid)
        match_id_map[(m['ronda'], m['idx'])] = cur.lastrowid

    # Vincular next_match_id
    for r in range(rounds-1):
        count = size // (2 ** (r+1))
        for i in range(count):
            curr_id = match_id_map[(r, i)]
            next_id = match_id_map[(r+1, i//2)]
            next_slot = 1 if i % 2 == 0 else 2
            conn.execute("UPDATE partidos SET next_match_id=?, next_slot=? WHERE id=?", (next_id, next_slot, curr_id))

    conn.commit()

    # Autofill byes
    byes = conn.execute("SELECT * FROM partidos WHERE ronda=0").fetchall()
    for b in byes:
        b = dict(b)
        if b['p1_id'] and not b['p2_id']:
            _set_winner(conn, b['id'], b['p1_id'])
        elif b['p2_id'] and not b['p1_id']:
            _set_winner(conn, b['id'], b['p2_id'])

    conn.commit()
    conn.close()
    return {"ok": True}

def _set_winner(conn, match_id, winner_id):
    m = dict(conn.execute("SELECT * FROM partidos WHERE id=?", (match_id,)).fetchone())
    conn.execute("UPDATE partidos SET winner_id=? WHERE id=?", (winner_id, match_id))
    if m['next_match_id']:
        if m['next_slot'] == 1:
            conn.execute("UPDATE partidos SET p1_id=? WHERE id=?", (winner_id, m['next_match_id']))
        else:
            conn.execute("UPDATE partidos SET p2_id=? WHERE id=?", (winner_id, m['next_match_id']))

@app.put("/api/partidos/{match_id}")
def update_partido(match_id: int, result: MatchResult):
    conn = get_db()
    m = conn.execute("SELECT * FROM partidos WHERE id=?", (match_id,)).fetchone()
    if not m:
        conn.close()
        raise HTTPException(404, "Partido no encontrado")
    conn.execute(
        "UPDATE partidos SET score1=?, score2=?, winner_id=? WHERE id=?",
        (json.dumps(result.score1), json.dumps(result.score2), result.winner_id, match_id)
    )
    if result.winner_id:
        _set_winner(conn, match_id, result.winner_id)
    conn.commit()
    conn.close()
    return {"ok": True}

# --- CONFIG PUNTOS ---
@app.get("/api/config/puntos")
def get_puntos():
    conn = get_db()
    row = conn.execute("SELECT valor FROM config WHERE clave='puntos'").fetchone()
    conn.close()
    return json.loads(row['valor'])

@app.put("/api/config/puntos")
def update_puntos(p: PuntosConfig):
    conn = get_db()
    conn.execute("UPDATE config SET valor=? WHERE clave='puntos'", (json.dumps(p.dict()),))
    conn.commit()
    conn.close()
    return {"ok": True}

# --- FRONTEND ---
@app.get("/")
def root():
    return FileResponse("static/index.html")

app.mount("/", StaticFiles(directory="static", html=True), name="static")