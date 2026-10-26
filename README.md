# Painel TSE — presidente 2026, 2º turno

Painel web para acompanhar em tempo real a apuração da **eleição presidencial de 2026 (2º turno)**,
usando os dados públicos oficiais do TSE: votos, percentuais, fotos dos candidatos, progresso das
seções, comparecimento/abstenção, evolução por estado e **mapa de todos os municípios do Brasil**,
cada cidade pintada com a cor do candidato mais votado nela.

O painel é dedicado a esse único pleito: não permite escolher outras eleições, cargos, UFs ou
municípios. O mapa cobre automaticamente todos os municípios do país.

## Como usar

Requisito: **Python 3.10 ou superior** (somente biblioteca padrão, sem `pip install`).

1. Dê um duplo clique em `iniciar.bat` **ou** execute no terminal:

   ```bat
   python painel.py
   ```

2. O navegador abre automaticamente em <http://localhost:8787>.
3. Deixe a janela do terminal aberta enquanto estiver acompanhando.

### Acessar de outro computador / celular na mesma rede

O `iniciar.bat` já sobe o servidor aceitando conexões da rede local (`--host 0.0.0.0`).
No terminal, ele mostra o endereço para usar, por exemplo:

```
Rede:    http://172.31.10.29:8787/
```

Se você iniciar manualmente com `python painel.py`, use:

```bat
python painel.py --host 0.0.0.0
```

> Na primeira vez, o Windows pode pedir para liberar o Python no firewall.
> Autorize em **Redes privadas e públicas** (o perfil da rede pode estar como “Pública”).
> Se não aparecer o aviso, crie uma regra de entrada para a porta TCP 8787 (veja abaixo).

Para liberar a porta no firewall (PowerShell **como administrador**):

```powershell
New-NetFirewallRule -DisplayName "Painel TSE 8787" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8787 -Profile Any
```

Se o painel estiver em outra porta, troque `8787` pelo número usado (por exemplo, `8788`).

### Opções

```bat
python painel.py --port 9000     :: outra porta
python painel.py --host 0.0.0.0  :: aceitar acesso pela rede local
python painel.py --no-browser    :: não abrir o navegador
```

## O que aparece no painel

- **Progresso da apuração** — seções totalizadas, percentual e status (aguardando / em andamento / concluída).
- **Indicadores** — comparecimento, abstenção, votos válidos, brancos, nulos e seções totalizadas.
- **Resultado por candidato** — foto oficial, número, partido, coligação, vice, votos, percentual e
  variação entre atualizações.
- **Distribuição dos votos** — gráfico de rosca com os principais candidatos.
- **Evolução da apuração** — histórico local de votos ao longo do tempo.
- **Acompanhamento por estado** — seções apuradas, progresso, comparecimento e abstenção por UF (e exterior).
- **Mapa por município** — os 5.570 municípios do Brasil coloridos pelo candidato que lidera os votos
  em cada cidade, com legenda, contagem de cidades, progresso da varredura e zoom/arraste. Ao passar
  o mouse (ou tocar) em uma cidade, aparece o candidato, o percentual, os votos e as seções apuradas.

A atualização automática é configurável (5 s, 10 s, 30 s, 60 s ou pausada) e a preferência fica
salva no navegador. O tema claro/escuro também é lembrado.

## Como funciona

`painel.py` sobe um servidor HTTP local que:

1. serve os arquivos da pasta `web/`;
2. faz **proxy** da API pública do TSE (`https://resultados.tse.jus.br/oficial`), contornando o CORS;
3. normaliza o JSON oficial para um formato enxuto;
4. guarda cache curto (3 s para votos, 5 s para o acompanhamento) e mantém a última resposta boa
   caso o TSE oscile.

O catálogo do TSE é filtrado no servidor para manter apenas a eleição presidencial de 2026, 2º turno
(`PLEITO_ALVO` em `painel.py`).

### Mapa municipal

O TSE não publica um arquivo agregado com os votos de todos os municípios: para o mapa, o servidor
consulta o arquivo `-u.json` de cada cidade em segundo plano. A varredura:

- prioriza as cidades com maior eleitorado;
- usa conexões keep-alive e 20 consultas simultâneas;
- reaproveita cidades já totalizadas e repete apenas as que ainda estão em apuração;
- começa automaticamente quando o painel é aberto e continua em ondas enquanto a apuração corre.

Por isso o mapa vai sendo preenchido aos poucos (normalmente em 1 a 3 minutos na primeira carga) e
se atualiza sozinho. Cidades sem votos computados ainda aparecem em cinza.

Os contornos vêm da malha municipal do IBGE. O arquivo compacto
`web/dados/municipios-br.json` é gerado por `ferramentas/gerar_malha.py` (projeção Mercator e
caminhos SVG otimizados, ~0,8 MB):

```bat
python ferramentas/gerar_malha.py
```

A malha pública do IBGE pode não incluir municípios criados muito recentemente; eles continuam
contando na legenda, mas podem ficar sem contorno no desenho.

## Estrutura

```
PainelTSE/
├─ painel.py            servidor local + proxy + normalização dos dados
├─ iniciar.bat          atalho para Windows
├─ README.md
├─ ferramentas/
│  └─ gerar_malha.py    gera a malha municipal compacta a partir do IBGE
└─ web/
   ├─ index.html        estrutura do painel
   ├─ styles.css        tema escuro/claro e responsividade
   ├─ app.js            atualização automática, gráficos, histórico e mapa
   ├─ dados/
   │  └─ municipios-br.json   contornos dos 5.570 municípios (gerado)
   └─ favicon.svg
```

## Observações

- Enquanto o TSE não divulgar os primeiros boletins, o painel mostra **“Aguardando o início da
  divulgação dos resultados”** com os candidatos zerados.
- Os dados vêm diretamente do TSE; este projeto é apenas informativo e **não tem vínculo oficial**
  com o Tribunal Superior Eleitoral.
- Em caso de falha de rede, o painel exibe a última leitura válida e tenta novamente sozinho.
