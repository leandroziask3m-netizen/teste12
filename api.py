"""
API do Sistema de Áreas — LeandroTec
--------------------------------------
Backend com suporte a:
- Autenticação JWT simples (HMAC-SHA256)
- Gerenciamento de áreas por usuário
- Registro e rastreamento de máquinas agrícolas via GPS
- Endpoint público para receber coordenadas de dispositivos GPS em campo

Persistência: JSON em disco (areas.json, usuarios.json, machines.json)
Senhas: hash SHA256 + salt, nunca em texto puro
Tokens de usuário: válidos por 12 horas
"""

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import json
import os
import time
import hmac
import base64
import hashlib
import secrets
from datetime import datetime

app = FastAPI(title="LeandroTec API", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

AREAS_FILE    = "areas.json"
USERS_FILE    = "usuarios.json"
SECRET_FILE   = "secret.key"
MACHINES_FILE = "machines.json"
TOKEN_TTL_SEGUNDOS = 60 * 60 * 12  # 12 horas


# ---------------------------------------------------------------------------
# Helpers de persistência
# ---------------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def load_areas():    return load_json(AREAS_FILE, [])
def save_areas(d):   save_json(AREAS_FILE, d)
def load_users():    return load_json(USERS_FILE, {})
def save_users(d):   save_json(USERS_FILE, d)
def load_machines(): return load_json(MACHINES_FILE, [])
def save_machines(d):save_json(MACHINES_FILE, d)


# ---------------------------------------------------------------------------
# Migração: áreas sem campo "owner" → primeiro usuário
# ---------------------------------------------------------------------------

def migrar_areas_sem_owner():
    areas    = load_areas()
    usuarios = load_users()
    primeiro = next(iter(usuarios), "sem_dono")
    alterou  = False
    for a in areas:
        if not a.get("owner"):
            a["owner"] = primeiro
            alterou = True
    if alterou:
        save_areas(areas)

migrar_areas_sem_owner()


# ---------------------------------------------------------------------------
# Hash de senha (SHA256 + salt)
# ---------------------------------------------------------------------------

def hash_password(password: str, salt: str = None) -> str:
    if salt is None:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return f"{salt}${h}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hash_password(password, salt) == stored


# ---------------------------------------------------------------------------
# Tokens de sessão (HMAC-SHA256, sem dependências externas)
# ---------------------------------------------------------------------------

def carregar_ou_criar_segredo() -> bytes:
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r", encoding="utf-8") as f:
            return f.read().strip().encode("utf-8")
    novo = secrets.token_hex(32)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(novo)
    return novo.encode("utf-8")

SECRET_KEY = carregar_ou_criar_segredo()

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")

def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)

def gerar_token(usuario: str) -> str:
    payload     = {"usuario": usuario, "exp": int(time.time()) + TOKEN_TTL_SEGUNDOS}
    payload_b64 = _b64url_encode(json.dumps(payload).encode("utf-8"))
    assinatura  = hmac.new(SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{assinatura}"

def verificar_token(token: str):
    try:
        payload_b64, assinatura = token.split(".", 1)
    except ValueError:
        return None
    assinatura_esperada = hmac.new(SECRET_KEY, payload_b64.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(assinatura, assinatura_esperada):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    if payload.get("exp", 0) < time.time():
        return None
    return payload.get("usuario")

def get_current_user(authorization: str = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="faça login para continuar")
    token   = authorization[len("Bearer "):].strip()
    usuario = verificar_token(token)
    if usuario is None:
        raise HTTPException(status_code=401, detail="sessão expirada, faça login novamente")
    return usuario


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------

class Area(BaseModel):
    nome: str
    img: str = ""
    lat: float
    lng: float
    tipo: Optional[str] = None

class Credenciais(BaseModel):
    username: str
    password: str

class MachineRegister(BaseModel):
    nome:   str
    tipo:   str = "Trator"
    gps_id: str

class MachineLocationUpdate(BaseModel):
    """
    Payload enviado pelo dispositivo GPS instalado na máquina.
    O dispositivo deve enviar este JSON via POST para /machines/update-location
    a cada intervalo configurado (ex: a cada 30 segundos ou 5 minutos).
    """
    gps_id:         str
    lat:            float
    lng:            float
    token:          str           # token de autenticação do dispositivo
    velocidade_kmh: Optional[float] = None
    operacao:       Optional[str]   = None  # "Pulverização", "Plantio", etc.


# ---------------------------------------------------------------------------
# Rota raiz
# ---------------------------------------------------------------------------

@app.get("/")
def home():
    return HTMLResponse("""
    <html><body style="font-family:sans-serif;padding:40px;background:#F2F4F1">
    <h2 style="color:#1B4332">LeandroTec API v2.0</h2>
    <p>API de gestão de áreas e monitoramento de máquinas agrícolas.</p>
    <p><a href="/docs">📋 Documentação interativa (Swagger)</a></p>
    </body></html>
    """)


# ---------------------------------------------------------------------------
# ÁREAS — individuais por usuário (campo "owner")
# ---------------------------------------------------------------------------

@app.get("/areas")
def get_areas(usuario: str = Depends(get_current_user)):
    areas = load_areas()
    return [a for a in areas if a.get("owner") == usuario]


@app.post("/areas")
def add_area(area: Area, usuario: str = Depends(get_current_user)):
    areas   = load_areas()
    next_id = (max((a["id"] for a in areas), default=0)) + 1
    new_area = {
        "id":    next_id,
        "nome":  area.nome,
        "img":   area.img,
        "lat":   area.lat,
        "lng":   area.lng,
        "tipo":  area.tipo,
        "owner": usuario,
        "criado_em": datetime.utcnow().isoformat()
    }
    areas.append(new_area)
    save_areas(areas)
    return new_area


@app.delete("/areas/{area_id}")
def delete_area(area_id: int, usuario: str = Depends(get_current_user)):
    areas = load_areas()
    alvo  = next((a for a in areas if a["id"] == area_id), None)
    if alvo is None:
        raise HTTPException(status_code=404, detail="área não encontrada")
    if alvo.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="essa área não pertence a este usuário")
    areas = [a for a in areas if a["id"] != area_id]
    save_areas(areas)
    return {"msg": "removido"}


# ---------------------------------------------------------------------------
# MÁQUINAS — cadastro e monitoramento GPS
# ---------------------------------------------------------------------------

@app.get("/machines")
def get_machines(usuario: str = Depends(get_current_user)):
    """Retorna todas as máquinas registradas pelo usuário logado."""
    machines = load_machines()
    return [m for m in machines if m.get("owner") == usuario]


@app.post("/machines")
def add_machine(data: MachineRegister, usuario: str = Depends(get_current_user)):
    """Registra uma nova máquina agrícola."""
    machines = load_machines()

    if any(m["gps_id"] == data.gps_id and m["owner"] == usuario for m in machines):
        raise HTTPException(status_code=409, detail="já existe uma máquina com esse GPS ID nesta conta")

    # Gera token de dispositivo (usado pelo hardware GPS para autenticar envios)
    device_token = "lt_" + secrets.token_hex(24)

    new_machine = {
        "id":           secrets.token_hex(8),
        "nome":         data.nome,
        "tipo":         data.tipo,
        "gps_id":       data.gps_id,
        "owner":        usuario,
        "device_token": device_token,
        "lat":          None,
        "lng":          None,
        "velocidade_kmh": None,
        "operacao":     None,
        "last_seen":    None,
        "registrado_em": datetime.utcnow().isoformat()
    }

    machines.append(new_machine)
    save_machines(machines)

    return new_machine


@app.delete("/machines/{machine_id}")
def delete_machine(machine_id: str, usuario: str = Depends(get_current_user)):
    machines = load_machines()
    alvo = next((m for m in machines if m["id"] == machine_id), None)
    if alvo is None:
        raise HTTPException(status_code=404, detail="máquina não encontrada")
    if alvo.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="esta máquina não pertence a este usuário")
    machines = [m for m in machines if m["id"] != machine_id]
    save_machines(machines)
    return {"msg": "máquina removida"}


@app.post("/machines/update-location")
def update_machine_location(data: MachineLocationUpdate):
    """
    Endpoint público que recebe as coordenadas GPS da máquina em campo.

    O dispositivo GPS instalado na máquina deve chamar este endpoint
    periodicamente (ex: a cada 30s, 1min ou 5min) enviando o JSON:
    {
        "gps_id": "GPS-001",
        "lat": -18.684,
        "lng": -49.590,
        "token": "<device_token gerado no cadastro>",
        "velocidade_kmh": 12.4,
        "operacao": "Pulverização"
    }

    Autenticação: o campo "token" deve conter o device_token gerado quando
    a máquina foi cadastrada. Tokens "simulador" são aceitos para testes.
    """
    machines = load_machines()
    machine  = next((m for m in machines if m["gps_id"] == data.gps_id), None)

    if machine is None:
        raise HTTPException(status_code=404, detail="dispositivo GPS não registrado neste sistema")

    # Valida token (aceita "simulador" para facilitar testes)
    if data.token != "simulador" and machine.get("device_token") != data.token:
        raise HTTPException(status_code=403, detail="token de dispositivo inválido")

    # Atualiza posição e status
    machine["lat"]             = data.lat
    machine["lng"]             = data.lng
    machine["velocidade_kmh"]  = data.velocidade_kmh
    machine["operacao"]        = data.operacao
    machine["last_seen"]       = datetime.utcnow().isoformat()

    save_machines(machines)

    return {
        "msg":      "posição atualizada",
        "gps_id":   data.gps_id,
        "lat":      data.lat,
        "lng":      data.lng,
        "timestamp": machine["last_seen"]
    }


@app.get("/machines/{machine_id}/token")
def get_machine_token(machine_id: str, usuario: str = Depends(get_current_user)):
    """Retorna o device_token de uma máquina para configurar o dispositivo GPS."""
    machines = load_machines()
    machine  = next((m for m in machines if m["id"] == machine_id), None)
    if machine is None:
        raise HTTPException(status_code=404, detail="máquina não encontrada")
    if machine.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="acesso negado")
    return {"device_token": machine["device_token"], "gps_id": machine["gps_id"]}


@app.post("/machines/{machine_id}/regenerate-token")
def regenerate_machine_token(machine_id: str, usuario: str = Depends(get_current_user)):
    """Gera um novo device_token para a máquina (invalida o anterior)."""
    machines = load_machines()
    machine  = next((m for m in machines if m["id"] == machine_id), None)
    if machine is None:
        raise HTTPException(status_code=404, detail="máquina não encontrada")
    if machine.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="acesso negado")
    machine["device_token"] = "lt_" + secrets.token_hex(24)
    save_machines(machines)
    return {"device_token": machine["device_token"], "msg": "token regenerado com sucesso"}


# ---------------------------------------------------------------------------
# AUTENTICAÇÃO
# ---------------------------------------------------------------------------

@app.post("/register")
def register(data: Credenciais):
    user     = data.username.strip().lower()
    password = data.password

    if not user or not password:
        raise HTTPException(status_code=400, detail="usuário e senha são obrigatórios")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="a senha precisa ter pelo menos 4 caracteres")

    usuarios = load_users()
    if user in usuarios:
        raise HTTPException(status_code=409, detail="usuário já existe")

    usuarios[user] = hash_password(password)
    save_users(usuarios)

    return {"msg": "usuario criado", "user": user}


@app.post("/login")
def login(data: Credenciais):
    user     = data.username.strip().lower()
    password = data.password
    usuarios = load_users()
    stored   = usuarios.get(user)

    if stored and verify_password(password, stored):
        token = gerar_token(user)
        return {"msg": "login ok", "user": user, "token": token}

    raise HTTPException(status_code=401, detail="usuário ou senha inválidos")
