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
├── dashboard.html    → Painel principal (inclui seção "Manejo agronômico")
├── logo.png         → Logo da empresa
├── requirements.txt
├── areas.json       → Banco de dados de áreas (campos data_plantio e ciclo_meses são opcionais, usados pelo manejo agronômico)
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

### 🧪 Manejo agronômico — janela de aplicação e recomendação para cana-de-açúcar

Nova seção **"Manejo agronômico"** no painel, voltada para talhões de
cana-de-açúcar (cana-planta ou cana-soca).

**Como funciona:**
1. Ao cadastrar (ou editar) uma área com cultura "Cana-de-açúcar", informe a
   **data de plantio ou do último corte** e o **ciclo** (12 ou 18 meses).
2. O backend calcula os **dias após plantio/corte (DAP)** e classifica o
   talhão em um dos 4 estágios fenológicos: Brotação, Perfilhamento,
   Crescimento de colmos ou Maturação.
3. O backend busca o clima atual (Open-Meteo) e avalia a **janela de
   aplicação** com base em vento, chuva (atual e prevista nas próximas
   horas), umidade e temperatura.
4. O resultado é um semáforo — **Favorável / Atenção / Não aplicar** — com
   os motivos explicados, mais a **categoria de manejo recomendada**
   (defensivo, adubação e cuidados) para o estágio atual da cana.

**Limiares usados na janela de aplicação** (baseados em boas práticas
amplamente adotadas no manejo fitossanitário — ajustáveis em `api.py`,
função `avaliar_janela_aplicacao`):
- Vento acima de 15 km/h → não aplicar (alto risco de deriva)
- Vento entre 10–15 km/h ou abaixo de 3 km/h → atenção
- Chuva no momento → não aplicar
- Previsão de mais de 2 mm nas próximas 6h → atenção
- Umidade relativa abaixo de 40% → atenção
- Temperatura acima de 33°C → atenção

**Endpoints novos:**
```
PATCH /areas/{id}/agronomico     → grava data_plantio + ciclo_meses
GET   /areas/{id}/recomendacao   → calcula estágio + clima + recomendação
```

**Importante — limitações da ferramenta:**
Esta é uma ferramenta de **apoio à decisão baseada em regras gerais**. Ela
não usa dados de solo, histórico de pragas/doenças da área nem o plano de
manejo específico da usina, e não substitui a avaliação de um agrônomo
responsável. As categorias de defensivo recomendadas são genéricas (ex:
"herbicida pós-emergente seletivo") — a escolha do produto comercial
registrado deve respeitar a bula e o receituário agronômico obrigatório.

### 🔑 Segurança
- Senhas com SHA256 + salt único por usuário
- Tokens de sessão HMAC-SHA256 com expiração de 12h
- Tokens de dispositivo separados dos tokens de usuário
- Cada usuário só acessa e gerencia seus próprios dados

> **Atenção (pendências de segurança conhecidas, fora do escopo desta
> atualização):** o arquivo `usuarios.json` contém hashes reais e não deve
> ser versionado nem compartilhado; o token `"simulador"` aceito sem
> autenticação em `/machines/update-location` deve ser removido antes de
> produção; e o CORS está liberado para `allow_origins=["*"]` junto com
> `allow_credentials=True`, o que não é recomendado. Nenhuma dessas
> pendências foi alterada nesta atualização a pedido do projeto — avalie
> corrigi-las antes de expor a API publicamente.

---

## Hospedagem no Render

1. Suba o projeto para um repositório GitHub
2. Crie um Web Service no Render apontando para o repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `uvicorn api:app --host 0.0.0.0 --port $PORT`
5. Troque `const API = "http://127.0.0.1:8000"` nos dois HTML pela URL pública do Render

**Atenção:** o arquivo `secret.key` não deve ser versionado no git. No Render, configure como variável de ambiente ou deixe o sistema gerar automaticamente (tokens são invalidados a cada redeploy se o arquivo não persistir).
