"""
API do Sistema de Áreas — LeandroTec
--------------------------------------
Backend com suporte a:
- Autenticação JWT simples (HMAC-SHA256)
- Gerenciamento de áreas por usuário
- Registro e rastreamento de máquinas agrícolas via GPS
- Endpoint público para receber coordenadas de dispositivos GPS em campo
- Janela de aplicação agrícola e recomendação de manejo para cana-de-açúcar

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
import urllib.request
import urllib.error
from datetime import datetime, date

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
# MÓDULO AGRONÔMICO — estágio da cana, janela de aplicação e recomendação
# ---------------------------------------------------------------------------
#
# Referências usadas para as faixas de dias-após-plantio/corte (DAP) por fase:
#   - CHBAGRO, "Ciclo da Cana de Açúcar": brotação até ~30 DAP, perfilhamento
#     ~40-120 DAP, crescimento de colmos ~120-270 DAP, maturação ~270-360 DAP.
#   - Embrapa, "Fenologia — Cana-de-açúcar": emergência do broto em 20-30 DAP.
#   - Para cana-soca (rebrota após corte), a contagem usa a data do corte
#     como referência (DAP = dias desde o corte).
#
# Esta é uma ferramenta de apoio à decisão baseada em REGRAS GERAIS, não
# substitui a avaliação de um engenheiro agrônomo responsável pela área,
# nem leva em conta análise de solo, histórico de pragas/doenças ou o plano
# de manejo específico da usina. Sempre siga a bula do produto e a legislação
# vigente (receituário agronômico obrigatório para defensivos).

ESTAGIOS_CANA = [
    {"id": "brotacao",      "nome": "Brotação",             "dap_min": 0,   "dap_max": 30},
    {"id": "perfilhamento", "nome": "Perfilhamento",        "dap_min": 31,  "dap_max": 120},
    {"id": "crescimento",   "nome": "Crescimento de colmos","dap_min": 121, "dap_max": 270},
    {"id": "maturacao",     "nome": "Maturação",            "dap_min": 271, "dap_max": 10_000},
]

# Categorias de manejo recomendadas por estágio (princípios ativos como
# CATEGORIA/classe, não marca comercial — a escolha do produto registrado
# deve ser validada pelo agrônomo responsável e pela bula).
RECOMENDACAO_POR_ESTAGIO = {
    "brotacao": {
        "foco": "Proteção da brotação e controle inicial de plantas daninhas",
        "defensivo": "Herbicida pré-emergente (residual), aplicado em solo limpo e ainda sem daninhas emergidas",
        "adubacao": "Adubação de plantio/base (NPK conforme análise de solo), se ainda não realizada",
        "cuidado": "Evitar pisoteio/compactação sobre a linha de plantio; checar umidade do solo antes de aplicar residual"
    },
    "perfilhamento": {
        "foco": "Controle de plantas daninhas em pós-emergência e formação da touceira",
        "defensivo": "Herbicida pós-emergente seletivo (gramíneas e folhas largas), conforme infestação observada em campo",
        "adubacao": "Adubação de cobertura nitrogenada — fase de maior demanda de N para perfilhamento",
        "cuidado": "Evitar excesso de N em solos já bem providos; observar sintomas de deficiência antes de definir dose"
    },
    "crescimento": {
        "foco": "Sustentar o crescimento dos colmos e monitorar pragas/doenças",
        "defensivo": "Inseticida/fungicida apenas mediante monitoramento de pragas (ex: broca-da-cana, cigarrinha) — evitar aplicação preventiva sem necessidade constatada",
        "adubacao": "Complementação de potássio se indicado pela análise foliar/solo",
        "cuidado": "Evitar aplicações de herbicida foliar que possam causar fitotoxicidade no estágio de maior crescimento"
    },
    "maturacao": {
        "foco": "Maximizar o acúmulo de sacarose (ATR) e preparar a colheita",
        "defensivo": "Evitar novas aplicações de herbicida/adubo nitrogenado — risco de prolongar o crescimento vegetativo e reduzir o teor de açúcar",
        "adubacao": "Suspender adubação nitrogenada; maturador químico só com indicação técnica e dentro da janela pré-colheita",
        "cuidado": "Priorizar monitoramento e logística de colheita; aplicações nesta fase exigem validação agronômica específica"
    },
}


def calcular_estagio_cana(data_plantio_str: str, ciclo_meses: int = 12):
    """Calcula o estágio fenológico da cana com base nos dias desde o
    plantio (ou último corte, no caso de cana-soca)."""
    try:
        data_plantio = datetime.strptime(data_plantio_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

    hoje = date.today()
    dap  = (hoje - data_plantio).days  # dias após plantio/corte

    if dap < 0:
        return {"dap": dap, "estagio_id": None, "estagio_nome": "Data futura", "dentro_do_ciclo": False}

    ciclo_dias = (ciclo_meses or 12) * 30
    dentro_do_ciclo = dap <= ciclo_dias

    estagio = next(
        (e for e in ESTAGIOS_CANA if e["dap_min"] <= dap <= e["dap_max"]),
        ESTAGIOS_CANA[-1]
    )

    return {
        "dap": dap,
        "estagio_id": estagio["id"],
        "estagio_nome": estagio["nome"],
        "ciclo_dias_estimado": ciclo_dias,
        "dentro_do_ciclo": dentro_do_ciclo,
    }


def avaliar_janela_aplicacao(clima_atual: dict, clima_diario: Optional[dict] = None):
    """
    Avalia se as condições climáticas atuais favorecem a aplicação de
    defensivos/adubo, com base em limiares amplamente usados no manejo
    fitossanitário:
      - Vento acima de ~15 km/h aumenta risco de deriva.
      - Vento muito baixo (<3 km/h) pode indicar inversão térmica.
      - Chuva nas próximas horas lava o produto antes da absorção.
      - Umidade relativa muito baixa (<40%) reduz eficácia de muitos produtos.
    Retorna um status (favoravel / atencao / nao_aplicar) e os motivos.
    """
    vento   = clima_atual.get("wind_speed_10m")
    chuva   = clima_atual.get("precipitation")
    umidade = clima_atual.get("relative_humidity_2m")
    temp    = clima_atual.get("temperature_2m")

    motivos = []
    nivel = "favoravel"  # favoravel | atencao | nao_aplicar

    def escalar(novo_nivel):
        nonlocal nivel
        ordem = {"favoravel": 0, "atencao": 1, "nao_aplicar": 2}
        if ordem[novo_nivel] > ordem[nivel]:
            nivel = novo_nivel

    if vento is not None:
        if vento > 15:
            escalar("nao_aplicar")
            motivos.append(f"Vento de {vento} km/h — acima de 15 km/h, alto risco de deriva do produto para fora do talhão.")
        elif vento > 10:
            escalar("atencao")
            motivos.append(f"Vento de {vento} km/h — próximo do limite recomendado (10-15 km/h), redobrar atenção à deriva.")
        elif vento < 3:
            escalar("atencao")
            motivos.append(f"Vento muito baixo ({vento} km/h) — risco de inversão térmica, que pode concentrar a deriva.")

    if chuva is not None and chuva > 0:
        escalar("nao_aplicar")
        motivos.append(f"Precipitação registrada agora ({chuva} mm) — produto pode ser lavado antes de agir.")

    if clima_diario:
        chuva_prevista = clima_diario.get("precipitation_proxima", None)
        if chuva_prevista is not None and chuva_prevista > 2:
            escalar("atencao")
            motivos.append(f"Previsão de {chuva_prevista} mm de chuva nas próximas horas — avalie postergar a aplicação.")

    if umidade is not None and umidade < 40:
        escalar("atencao")
        motivos.append(f"Umidade relativa baixa ({umidade}%) — pode reduzir a eficácia de absorção de alguns produtos.")

    if temp is not None and temp > 33:
        escalar("atencao")
        motivos.append(f"Temperatura elevada ({temp}°C) — maior risco de volatilização/evaporação do produto.")

    if not motivos:
        motivos.append("Vento, umidade e ausência de chuva dentro das faixas recomendadas para aplicação.")

    return {"nivel": nivel, "motivos": motivos}


def buscar_clima_atual(lat: float, lng: float):
    """Busca clima atual + previsão de chuva nas próximas horas via Open-Meteo."""
    url = (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lng}"
        "&current=temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation"
        "&hourly=precipitation"
        "&forecast_days=1&timezone=America/Sao_Paulo"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "LeandroTec/2.0 (+https://github.com)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        # Loga o motivo real no console do servidor (visível nos "Logs" do Render)
        # em vez de engolir o erro silenciosamente — facilita diagnosticar timeouts,
        # DNS, ou bloqueios de rede sem precisar adivinhar.
        print(f"[clima] falha ao buscar clima para ({lat}, {lng}): {type(e).__name__}: {e}")
        return None, None

    atual = data.get("current", {})

    chuva_proxima = None
    hourly = data.get("hourly", {})
    if hourly.get("precipitation"):
        # soma a chuva prevista nas próximas 6 horas a partir de agora
        chuva_proxima = round(sum(hourly["precipitation"][:6]), 1)

    return atual, {"precipitation_proxima": chuva_proxima}


# ---------------------------------------------------------------------------
# Modelos Pydantic
# ---------------------------------------------------------------------------

class Area(BaseModel):
    nome: str
    img: str = ""
    lat: float
    lng: float
    tipo: Optional[str] = None
    data_plantio: Optional[str] = None   # formato "AAAA-MM-DD" — data do plantio ou do último corte (cana-soca)
    ciclo_meses: Optional[int] = None    # 12 ou 18 — duração estimada do ciclo da cana

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
        "data_plantio": area.data_plantio,
        "ciclo_meses":  area.ciclo_meses,
        "owner": usuario,
        "criado_em": datetime.utcnow().isoformat()
    }
    areas.append(new_area)
    save_areas(areas)
    return new_area


@app.get("/areas/{area_id}/recomendacao")
def recomendacao_area(area_id: int, usuario: str = Depends(get_current_user)):
    """
    Avalia se a área está em condições favoráveis para aplicação de
    defensivos/adubo agora (com base no clima atual) e recomenda a
    categoria de manejo mais adequada ao estágio da cana-de-açúcar.

    Combina:
      - Estágio fenológico da cana (calculado a partir de data_plantio + ciclo_meses)
      - Condições climáticas atuais (Open-Meteo): vento, chuva, umidade, temperatura

    Não substitui a avaliação de um agrônomo responsável pela área.
    """
    areas = load_areas()
    area  = next((a for a in areas if a["id"] == area_id), None)
    if area is None:
        raise HTTPException(status_code=404, detail="área não encontrada")
    if area.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="essa área não pertence a este usuário")

    if not area.get("data_plantio"):
        raise HTTPException(
            status_code=400,
            detail="esta área ainda não tem data de plantio/corte cadastrada — informe em 'Recomendação Agronômica' para liberar a análise"
        )

    estagio = calcular_estagio_cana(area["data_plantio"], area.get("ciclo_meses") or 12)
    if estagio is None or estagio["estagio_id"] is None:
        raise HTTPException(status_code=400, detail="não foi possível calcular o estágio — verifique a data de plantio/corte cadastrada")

    clima_atual, clima_diario = buscar_clima_atual(area["lat"], area["lng"])
    if clima_atual is None:
        raise HTTPException(
            status_code=503,
            detail="não foi possível obter dados climáticos do serviço Open-Meteo agora. Verifique os 'Logs' do Render para o motivo exato, ou tente novamente em alguns minutos."
        )

    janela = avaliar_janela_aplicacao(clima_atual, clima_diario)
    manejo = RECOMENDACAO_POR_ESTAGIO[estagio["estagio_id"]]

    return {
        "area_id": area_id,
        "area_nome": area["nome"],
        "estagio": estagio,
        "clima": {
            "temperatura_c": clima_atual.get("temperature_2m"),
            "umidade_pct": clima_atual.get("relative_humidity_2m"),
            "vento_kmh": clima_atual.get("wind_speed_10m"),
            "precipitacao_mm": clima_atual.get("precipitation"),
            "chuva_prevista_6h_mm": clima_diario.get("precipitation_proxima") if clima_diario else None,
        },
        "janela_aplicacao": janela,
        "recomendacao_manejo": manejo,
        "aviso": "Recomendação baseada em regras gerais para cana-de-açúcar. Não substitui laudo de análise de solo nem a avaliação do agrônomo responsável pela área. Siga sempre a bula do produto e o receituário agronômico."
    }


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


class AreaAgronomico(BaseModel):
    data_plantio: str            # "AAAA-MM-DD" — plantio ou último corte
    ciclo_meses: int = 12        # 12 (cana de ano) ou 18 (cana de ano e meio)


@app.patch("/areas/{area_id}/agronomico")
def atualizar_dados_agronomicos(area_id: int, dados: AreaAgronomico, usuario: str = Depends(get_current_user)):
    """Atualiza a data de plantio/corte e o ciclo de uma área já cadastrada,
    necessários para calcular o estágio fenológico da cana."""
    areas = load_areas()
    alvo  = next((a for a in areas if a["id"] == area_id), None)
    if alvo is None:
        raise HTTPException(status_code=404, detail="área não encontrada")
    if alvo.get("owner") != usuario:
        raise HTTPException(status_code=403, detail="essa área não pertence a este usuário")
    try:
        datetime.strptime(dados.data_plantio, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="data_plantio deve estar no formato AAAA-MM-DD")
    if dados.ciclo_meses not in (12, 18):
        raise HTTPException(status_code=400, detail="ciclo_meses deve ser 12 ou 18")
    alvo["data_plantio"] = dados.data_plantio
    alvo["ciclo_meses"]  = dados.ciclo_meses
    save_areas(areas)
    return alvo


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
