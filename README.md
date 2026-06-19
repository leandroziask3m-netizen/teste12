# LeandroTec — Sistema de Gestão de Áreas Agrícolas

## Como rodar localmente

```bash
pip install -r requirements.txt
uvicorn api:app --reload
```

Acesse: `http://127.0.0.1:8000` — API  
Abra `index.html` no navegador para a interface.

---

## Estrutura dos arquivos

```
├── api.py           → Backend FastAPI (Python)
├── index.html       → Tela de login / cadastro
├── dashboard.html   → Painel principal
├── logo.png         → Logo da empresa
├── requirements.txt
├── areas.json       → Banco de dados de áreas (gerado automaticamente)
├── usuarios.json    → Banco de dados de usuários (gerado automaticamente)
├── machines.json    → Banco de dados de máquinas (gerado automaticamente)
└── secret.key       → Chave HMAC para tokens (gerada automaticamente, NÃO versionar no git)
```

---

## Novas funcionalidades v2.0

### 🚜 Máquinas em campo
- Cadastro de máquinas agrícolas (Trator, Colheitadeira, Pulverizador, etc.)
- Cada máquina recebe um **device_token** para autenticação do dispositivo GPS
- Status online/offline baseado no último sinal recebido (threshold: 10 minutos)
- Mapa dedicado mostrando a posição de todas as máquinas em tempo real

### 📡 Integração GPS
O dispositivo GPS instalado na máquina deve enviar um POST periódico para:

```
POST /machines/update-location
Content-Type: application/json

{
  "gps_id": "GPS-001",
  "lat": -18.684179,
  "lng": -49.590055,
  "token": "lt_abc123...",
  "velocidade_kmh": 12.4,
  "operacao": "Pulverizacao"
}
```

- **Intervalo recomendado:** 30 segundos a 5 minutos
- **token:** gerado no painel em Integração GPS → Token de máquina
- Token `"simulador"` é aceito para testes sem autenticar

### 🌤 Clima por área
- Consulta de temperatura, umidade, vento e precipitação de qualquer área cadastrada
- Previsão para os próximos 3 dias
- Usa a API Open-Meteo (gratuita, sem chave necessária)
- Clima também disponível diretamente nas máquinas (via coordenada GPS atual)

### 🔑 Segurança
- Senhas com SHA256 + salt único por usuário
- Tokens de sessão HMAC-SHA256 com expiração de 12h
- Tokens de dispositivo separados dos tokens de usuário
- Cada usuário só acessa e gerencia seus próprios dados

---

## Hospedagem no Render

1. Suba o projeto para um repositório GitHub
2. Crie um Web Service no Render apontando para o repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn api:app --host 0.0.0.0 --port $PORT`
5. Troque `const API = "http://127.0.0.1:8000"` nos dois HTML pela URL pública do Render

**Atenção:** o arquivo `secret.key` não deve ser versionado no git. No Render, configure como variável de ambiente ou deixe o sistema gerar automaticamente (tokens são invalidados a cada redeploy se o arquivo não persistir).
