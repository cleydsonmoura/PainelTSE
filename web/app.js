/* =========================================================================
   Painel TSE — presidente 2026, 2º turno
   Consome o proxy local (/api/*) que fala com resultados.tse.jus.br
   ========================================================================= */
'use strict';

const API = {
  config: '/api/config',
  resultado: '/api/resultado',
  acompanhamento: '/api/acompanhamento',
  mapa: '/api/mapa',
  malha: 'dados/municipios-br.json',
  foto: '/api/foto',
};

const CHAVE_INTERVALO = 'tse:intervalo';
const CHAVE_SERIE = 'tse:serie:';

const CORES_PARTIDO = {
  PT: '#e03131', PL: '#0b8315', PSD: '#0f8bd1', MDB: '#1f9d55',
  PSDB: '#2b7fd4', UNIAO: '#1d4ed8', 'UNIÃO': '#1d4ed8', PP: '#1f7a8c',
  REPUBLICANOS: '#2f6d3a', PDT: '#d9480f', PSB: '#c2255c', NOVO: '#e8590c',
  PSOL: '#7048e8', REDE: '#12b886', PCDOB: '#c92a2a', 'PC do B': '#c92a2a',
  PV: '#2f9e44', SOLIDARIEDADE: '#f08c00', CIDADANIA: '#0c8599',
  AVANTE: '#5f3dc4', PROS: '#8d6e63', PATRIOTA: '#556b2f', DC: '#343a40',
  PMB: '#495057', UP: '#862e9c', PSTU: '#a61e4d', PCO: '#7f1d1d',
  AGIR: '#0b7285', MOBILIZA: '#364fc7', PRD: '#087f5b', MISSÃO: '#1864ab',
};

const estado = {
  config: null,
  eleicao: null,
  cargo: '1',
  intervalo: 10000,
  timer: null,
  ocupado: false,
  ultimo: null,
  historico: {},
  chaveSerie: '',
  falhas: 0,
  sequencia: 0,
};

const mapa = {
  iniciado: false,
  malha: null,
  svg: null,
  camada: null,
  paths: new Map(),
  dados: new Map(),
  candidatos: [],
  cores: [],
  progresso: null,
  resumo: null,
  timer: null,
  ocupado: false,
  falhas: 0,
  view: null,
  arrasto: null,
  interacaoLigada: false,
  atualizadoEm: null,
  dicaCodigo: null,
  dicaTamanho: null,
};

const $ = (seletor) => document.querySelector(seletor);

/* ---------------------------------------------------------------- helpers */

const fmtInt = new Intl.NumberFormat('pt-BR');
const fmtDec = (valor, casas = 2) =>
  Number(valor || 0).toLocaleString('pt-BR', {
    minimumFractionDigits: casas,
    maximumFractionDigits: casas,
  });

const pct = (valor, casas = 2) => `${fmtDec(valor, casas)}%`;

function compacto(valor) {
  const n = Number(valor) || 0;
  if (n >= 1e9) return `${fmtDec(n / 1e9, 1)} bi`;
  if (n >= 1e6) return `${fmtDec(n / 1e6, 1)} mi`;
  if (n >= 1e3) return `${fmtInt.format(Math.round(n / 1e3))} mil`;
  return fmtInt.format(n);
}

function esc(valor) {
  return String(valor ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function corPartido(partido) {
  const chave = String(partido || '').toUpperCase().trim();
  if (CORES_PARTIDO[chave]) return CORES_PARTIDO[chave];
  let hash = 0;
  for (let i = 0; i < chave.length; i += 1) hash = (hash * 31 + chave.charCodeAt(i)) % 360;
  return `hsl(${hash} 68% 52%)`;
}

function iniciais(nome) {
  const partes = String(nome || '?')
    .split(/\s+/)
    .filter((p) => p.length > 2 && !['DOS', 'DAS', 'DE', 'DA', 'DO'].includes(p.toUpperCase()));
  return (partes[0]?.[0] || '?') + (partes[1]?.[0] || '');
}

function horaAgora() {
  return new Date().toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function horaCurta(ts) {
  return new Date(ts).toLocaleTimeString('pt-BR', { hour: '2-digit', minute: '2-digit' });
}

async function getJSON(url) {
  const controlador = new AbortController();
  const limite = setTimeout(() => controlador.abort(), 20000);
  try {
    const resposta = await fetch(url, { signal: controlador.signal, headers: { Accept: 'application/json' } });
    const texto = await resposta.text();
    let dados = null;
    try {
      dados = texto ? JSON.parse(texto) : null;
    } catch {
      throw new Error('Resposta inválida do servidor local.');
    }
    if (!resposta.ok || (dados && dados.erro)) {
      throw new Error((dados && dados.erro) || `Falha HTTP ${resposta.status}`);
    }
    return dados;
  } catch (erro) {
    if (erro.name === 'AbortError') throw new Error('O TSE demorou para responder. Tentando novamente em instantes.');
    throw erro;
  } finally {
    clearTimeout(limite);
  }
}

/* ------------------------------------------------------------- preferência */

function lerIntervalo() {
  const bruto = localStorage.getItem(CHAVE_INTERVALO);
  if (bruto === null) return 10000;
  const salvo = Number(bruto);
  return [0, 5000, 10000, 30000, 60000].includes(salvo) ? salvo : 10000;
}

function gravarIntervalo() {
  try {
    localStorage.setItem(CHAVE_INTERVALO, String(estado.intervalo));
  } catch { /* modo privado */ }
}

/* ------------------------------------------------------------- estado da UI */

function setLive(tipo, texto) {
  const live = $('#live');
  live.dataset.estado = tipo;
  $('#liveTexto').textContent =
    texto || { on: 'Ao vivo', off: 'Pausado', erro: 'Sem conexão' }[tipo] || '';
}

function mostrarBanner(mensagem) {
  const banner = $('#banner');
  $('#bannerTexto').textContent = mensagem;
  banner.hidden = false;
}

function esconderBanner() {
  $('#banner').hidden = true;
}

function toast(mensagem) {
  const el = $('#toast');
  el.textContent = mensagem;
  el.hidden = false;
  requestAnimationFrame(() => (el.dataset.visivel = '1'));
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {
    el.dataset.visivel = '0';
    setTimeout(() => (el.hidden = true), 250);
  }, 2600);
}

function skeleton(ativo) {
  $('#skeleton').hidden = !ativo;
  if (ativo) {
    document
      .querySelectorAll('.hero, .kpis, .layout, #painelAcomp')
      .forEach((el) => (el.style.visibility = 'hidden'));
  } else {
    document
      .querySelectorAll('.hero, .kpis, .layout, #painelAcomp')
      .forEach((el) => (el.style.visibility = ''));
  }
}

/* ------------------------------------------------------------------- tema */

function aplicarTema(tema) {
  document.documentElement.dataset.theme = tema;
  try {
    localStorage.setItem('tse:tema', tema);
  } catch { /* ignora */ }
}

function alternarTema() {
  aplicarTema(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
}

/* ----------------------------------------------------- carregar / renderizar */

function urlResultado() {
  const params = new URLSearchParams({
    ciclo: estado.eleicao.ciclo,
    eleicao: estado.eleicao.cd,
    cargo: estado.cargo,
    uf: 'br',
  });
  return `${API.resultado}?${params.toString()}`;
}

async function carregar(opcoes = {}) {
  if (estado.ocupado) return;

  estado.ocupado = true;
  estado.sequencia += 1;
  const sequencia = estado.sequencia;
  const botao = $('#btnAtualizar');
  botao.classList.add('spin');

  try {
    const dados = await getJSON(urlResultado());
    if (sequencia !== estado.sequencia) return; // uma consulta mais nova assumiu
    const anterior = estado.ultimo;
    estado.ultimo = dados;
    estado.falhas = 0;

    renderTudo(dados, anterior);
    registrarSerie(dados);

    setLive(estado.intervalo ? 'on' : 'off');
    esconderBanner();
    if (opcoes.manual) toast('Dados atualizados');
  } catch (erro) {
    if (sequencia !== estado.sequencia) return;
    estado.falhas += 1;
    setLive('erro');
    mostrarBanner(`${erro.message} O painel tentará novamente automaticamente.`);
    if (estado.ultimo) marcarDesatualizado();
  } finally {
    if (sequencia === estado.sequencia) {
      estado.ocupado = false;
      botao.classList.remove('spin');
      agendar();
    }
  }
}

async function carregarAcompanhamento() {
  if (!estado.eleicao) return;
  try {
    const dados = await getJSON(
      `${API.acompanhamento}?ciclo=${encodeURIComponent(estado.eleicao.ciclo)}&eleicao=${encodeURIComponent(
        estado.eleicao.cd,
      )}&uf=br`,
    );
    renderAcompanhamento(dados);
  } catch {
    $('#painelAcomp').hidden = false;
    $('#tabelaAcomp').innerHTML =
      '<p class="hint" style="margin:16px 20px">Não foi possível carregar o acompanhamento por estado.</p>';
  }
}

function agendar() {
  clearTimeout(estado.timer);
  if (!estado.intervalo) return;
  estado.timer = setTimeout(() => {
    if (document.hidden) agendar();
    else carregar();
  }, estado.intervalo);
}

function marcarDesatualizado() {
  const nota = $('#syncNota');
  nota.textContent = `sem conexão há ${estado.falhas} tentativa(s) · exibindo última leitura`;
}

/* --------------------------------------------------------------- render */

function renderTudo(dados, anterior) {
  renderHero(dados);
  renderKpis(dados);
  renderCandidatos(dados, anterior);
  renderDonut(dados);
  renderLinha();
  $('#tseStamp').textContent = dados.geradoEm || '—';
  $('#syncStamp').textContent = horaAgora();
  $('#syncNota').textContent = dados.cache?.stale
    ? `cache do painel (${Math.round(dados.cache.idadeSegundos || 0)}s)`
    : 'consulta direta à API do TSE';
}

function renderHero(dados) {
  const eleicao = estado.eleicao;
  $('#heroAno').textContent = eleicao.ano;
  $('#heroTurno').textContent = `${dados.turno || eleicao.turno}º turno`;
  $('#heroTipo').textContent = 'Eleição presidencial';
  $('#heroCargo').textContent = dados.cargo.nome || 'Presidente';
  $('#heroUf').textContent = 'Brasil';

  const secoes = dados.secoes;
  const totalVotos = dados.votos.total;

  let status;
  if (dados.situacaoApuracao === 'aguardando' || totalVotos === 0) {
    status = 'Aguardando o início da divulgação dos resultados desta eleição.';
  } else if (dados.situacaoApuracao === 'finalizada') {
    status = `Apuração concluída · ${pct(secoes.pct)} das seções totalizadas.`;
  } else {
    status = `Apuração em andamento · ${pct(secoes.pct)} das seções totalizadas.`;
  }
  $('#heroStatus').textContent = status;

  const barra = $('#heroBar');
  barra.style.width = `${Math.min(100, Math.max(0, secoes.pct))}%`;
  $('#heroProgresso').setAttribute('aria-valuenow', String(Math.round(secoes.pct)));
  $('#heroPct').textContent = pct(secoes.pct);
  $('#heroSecoes').textContent = `${fmtInt.format(secoes.totalizadas)} de ${fmtInt.format(
    secoes.total,
  )} seções apuradas`;
}

function renderKpis(dados) {
  const { eleitorado, votos, secoes } = dados;

  const itens = [
    {
      rotulo: 'Comparecimento',
      valor: fmtInt.format(eleitorado.comparecimento),
      pct: eleitorado.pctComparecimento,
      cor: 'var(--brand)',
      nota: `${pct(eleitorado.pctComparecimento)} do eleitorado apto`,
    },
    {
      rotulo: 'Abstenção',
      valor: fmtInt.format(eleitorado.abstencao),
      pct: eleitorado.pctAbstencao,
      cor: 'var(--warn)',
      nota: `${pct(eleitorado.pctAbstencao)} dos aptos a votar`,
    },
    {
      rotulo: 'Votos válidos',
      valor: fmtInt.format(votos.validos),
      pct: votos.pctValidos,
      cor: 'var(--ok)',
      nota: `${pct(votos.pctValidos)} dos votos apurados`,
      barra: false,
    },
    {
      rotulo: 'Votos em branco',
      valor: fmtInt.format(votos.brancos),
      pct: votos.pctBrancos,
      cor: '#8b9dc3',
      nota: `${pct(votos.pctBrancos)} do total`,
      barra: false,
    },
    {
      rotulo: 'Votos nulos',
      valor: fmtInt.format(votos.nulos),
      pct: votos.pctNulos,
      cor: 'var(--bad)',
      nota: `${pct(votos.pctNulos)} do total`,
      barra: false,
    },
    {
      rotulo: 'Seções totalizadas',
      valor: fmtInt.format(secoes.totalizadas),
      pct: secoes.pct,
      cor: 'var(--brand-2)',
      nota: `de ${fmtInt.format(secoes.total)} seções`,
    },
  ];

  $('#kpis').innerHTML = itens
    .map(
      (item) => `
      <article class="kpi" style="--kpi-cor:${item.cor}">
        <span class="kpi-label">${esc(item.rotulo)}</span>
        <div class="kpi-valor">
          <b>${esc(item.valor)}</b>
          <span>${pct(item.pct)}</span>
        </div>
        <div class="kpi-barra"><i data-largura="${Math.min(100, Math.max(0, item.pct))}"></i></div>
        <div class="kpi-nota">${esc(item.nota)}</div>
      </article>`,
    )
    .join('');

  requestAnimationFrame(() => {
    document.querySelectorAll('.kpi-barra i').forEach((barra) => {
      barra.style.width = `${barra.dataset.largura}%`;
    });
  });
}

function renderCandidatos(dados, anterior) {
  const lista = $('#listaCandidatos');
  const candidatos = dados.candidatos || [];

  $('#rankingSub').textContent = dados.votos.validos
    ? `${fmtInt.format(dados.votos.validos)} votos válidos computados`
    : 'Nenhum voto computado até o momento';

  const chips = [];
  chips.push(
    `<span class="chip" data-tone="${dados.situacaoApuracao === 'finalizada' ? 'ok' : 'warn'}">
       ${dados.situacaoApuracao === 'finalizada' ? 'Apuração concluída' : dados.situacaoApuracao === 'aguardando' ? 'Aguardando início' : 'Em andamento'}
     </span>`,
  );
  if (candidatos.length) chips.push(`<span class="chip"><strong>${candidatos.length}</strong> candidatos</span>`);
  $('#rankingChips').innerHTML = chips.join('');

  if (!candidatos.length) {
    lista.innerHTML = `
      <div class="vazio">
        <div class="pulso"></div>
        <h3>Sem candidatos publicados</h3>
        <p>O TSE ainda não divulgou os candidatos desta eleição.</p>
        <button class="btn" type="button" onclick="window.painelAtualizar()">Consultar novamente</button>
      </div>`;
    return;
  }

  const anteriorMapa = new Map((anterior?.candidatos || []).map((c) => [c.sqcand || c.nomeUrna, c.votos]));
  const maxPct = Math.max(...candidatos.map((c) => c.pct), 1);
  const ehSenador = String(dados.cargo.nome || '').toLowerCase().includes('senador');

  lista.innerHTML = candidatos
    .map((candidato, indice) => {
      const cor = corPartido(candidato.partido);
      const chave = candidato.sqcand || candidato.nomeUrna;
      const antes = anteriorMapa.get(chave);
      const delta = typeof antes === 'number' ? candidato.votos - antes : 0;
      const largura = Math.min(100, (candidato.pct / maxPct) * 100);
      const foto = candidato.sqcand
        ? `${API.foto}?ciclo=${encodeURIComponent(estado.eleicao.ciclo)}&eleicao=${encodeURIComponent(
            estado.eleicao.cd,
          )}&uf=br&id=${encodeURIComponent(candidato.sqcand)}`
        : '';

      const selo = seloCandidato(candidato);
      const vice = (candidato.vices || [])[0];
      const viceTexto = vice
        ? `${ehSenador ? 'Suplente' : 'Vice'}: ${vice.nome}${vice.partido ? ` (${vice.partido})` : ''}`
        : '';

      return `
      <article class="cand" data-pos="${indice + 1}" style="--cand-cor:${cor}">
        <div class="pos">${indice + 1}</div>
        <div class="foto" data-iniciais="${esc(iniciais(candidato.nomeUrna))}">
          ${
            foto
              ? `<img data-foto loading="lazy" alt="Foto de ${esc(candidato.nomeUrna)}" src="${foto}" />`
              : esc(iniciais(candidato.nomeUrna))
          }
        </div>
        <div class="cand-info">
          <div class="cand-nome">
            <b title="${esc(candidato.nome)}">${esc(candidato.nomeUrna)}</b>
            <span class="cand-num">${esc(candidato.numero || '—')}</span>
          </div>
          <div class="cand-meta">
            <span class="pt">${esc(candidato.partido || '—')}</span>
            ${candidato.coligacao ? ` · ${esc(candidato.coligacao)}` : ''}
          </div>
          <div class="cand-barra"><i data-largura="${largura}"></i></div>
          ${viceTexto ? `<div class="cand-vice">${esc(viceTexto)}</div>` : ''}
        </div>
        <div class="cand-numeros">
          <div class="cand-votos">${fmtInt.format(candidato.votos)}</div>
          <div class="cand-pct">${pct(candidato.pct)}</div>
          <div class="cand-extra">
            ${selo ? `<span class="badge" data-tipo="${selo.tipo}">${esc(selo.texto)}</span>` : ''}
            <span class="delta" data-visivel="${delta > 0 ? '1' : '0'}" data-delta="${chave}">
              ${delta > 0 ? `+${fmtInt.format(delta)}` : ''}
            </span>
          </div>
        </div>
      </article>`;
    })
    .join('');

  requestAnimationFrame(() => {
    lista.querySelectorAll('.cand-barra i').forEach((barra) => {
      barra.style.width = `${barra.dataset.largura}%`;
    });
  });

  lista.querySelectorAll('img[data-foto]').forEach((img) => {
    img.addEventListener(
      'error',
      () => {
        const caixa = img.parentElement;
        caixa.classList.add('sem-foto');
        caixa.textContent = caixa.dataset.iniciais || '?';
      },
      { once: true },
    );
  });

  lista.querySelectorAll('.cand-votos').forEach((el, indice) => {
    const chave = candidatos[indice]?.sqcand || candidatos[indice]?.nomeUrna;
    const delta = document.querySelector(`.delta[data-delta="${CSS.escape(chave)}"]`);
    if (delta?.dataset.visivel === '1' && typeof el.animate === 'function') {
      el.animate(
        [
          { color: 'var(--ok)' },
          { color: 'inherit' },
        ],
        { duration: 1100, easing: 'ease-out' },
      );
    }
  });
}

function seloCandidato(candidato) {
  const situacao = String(candidato.situacao || '').toLowerCase();
  if (candidato.eleito) return { tipo: 'eleito', texto: 'Eleito' };
  if (situacao.includes('turno')) return { tipo: 'turno', texto: candidato.situacao };
  if (!situacao) return null;
  if (situacao.includes('eleito')) return { tipo: 'fora', texto: 'Não eleito' };
  return { tipo: 'fora', texto: candidato.situacao };
}

function renderDonut(dados) {
  const alvo = $('#donut');
  const candidatos = (dados.candidatos || []).filter((c) => c.votos > 0);
  const total = candidatos.reduce((soma, c) => soma + c.votos, 0);

  if (!total) {
    alvo.innerHTML = `
      <div class="vazio" style="padding:28px 8px">
        <h3 style="font-size:15px">Sem votos computados</h3>
        <p>A distribuição aparecerá assim que o TSE divulgar os primeiros boletins.</p>
      </div>`;
    return;
  }

  const principais = candidatos.slice(0, 5);
  const outros = candidatos.slice(5);
  const segmentos = principais.map((c) => ({
    nome: c.nomeUrna,
    cor: corPartido(c.partido),
    valor: c.votos,
    pct: c.pct,
  }));
  if (outros.length) {
    const votosOutros = outros.reduce((soma, c) => soma + c.votos, 0);
    segmentos.push({
      nome: 'Outros',
      cor: '#5b6b82',
      valor: votosOutros,
      pct: (votosOutros / total) * 100,
    });
  }

  const raio = 62;
  const circunferencia = 2 * Math.PI * raio;
  let deslocamento = 0;

  const arcos = segmentos
    .map((segmento) => {
      const fracao = segmento.valor / total;
      const comprimento = fracao * circunferencia;
      const arco = `
        <circle cx="80" cy="80" r="${raio}" fill="none"
          stroke="${segmento.cor}" stroke-width="16"
          stroke-dasharray="${comprimento.toFixed(2)} ${(circunferencia - comprimento).toFixed(2)}"
          stroke-dashoffset="${(-deslocamento).toFixed(2)}"
          transform="rotate(-90 80 80)" stroke-linecap="butt">
          <title>${esc(segmento.nome)}: ${fmtInt.format(segmento.valor)} votos (${pct(segmento.pct)})</title>
        </circle>`;
      deslocamento += comprimento;
      return arco;
    })
    .join('');

  alvo.innerHTML = `
    <div class="donut">
      <svg viewBox="0 0 160 160" role="img" aria-label="Distribuição dos votos entre os candidatos">
        <circle cx="80" cy="80" r="${raio}" fill="none" stroke="var(--line)" stroke-width="16" opacity=".55" />
        ${arcos}
        <text class="centro-valor" x="80" y="78" text-anchor="middle">${compacto(dados.votos.validos || total)}</text>
        <text class="centro-label" x="80" y="94" text-anchor="middle">votos válidos</text>
      </svg>
      <div class="legenda">
        ${segmentos
          .map(
            (segmento) => `
          <div class="item">
            <span class="cor" style="background:${segmento.cor}"></span>
            <span class="nome" title="${esc(segmento.nome)}">${esc(segmento.nome)}</span>
            <span class="valor">${pct(segmento.pct)}</span>
          </div>`,
          )
          .join('')}
      </div>
    </div>`;
}

/* ------------------------------------------------------------- histórico */

function chaveSerieAtual() {
  return `${CHAVE_SERIE}${estado.eleicao?.cd || ''}:${estado.cargo || ''}`;
}

function carregarSerie() {
  estado.chaveSerie = chaveSerieAtual();
  try {
    const bruto = JSON.parse(localStorage.getItem(estado.chaveSerie) || '{}');
    const agora = Date.now();
    const limpo = {};
    Object.entries(bruto || {}).forEach(([chave, serie]) => {
      if (!serie || !Array.isArray(serie.pontos)) return;
      const pontos = serie.pontos.filter(([t]) => agora - t < 8 * 3600 * 1000).slice(-240);
      if (pontos.length) limpo[chave] = { ...serie, pontos };
    });
    estado.historico = limpo;
  } catch {
    estado.historico = {};
  }
}

function registrarSerie(dados) {
  const agora = Date.now();
  (dados.candidatos || []).slice(0, 6).forEach((candidato) => {
    const chave = candidato.sqcand || candidato.nomeUrna;
    const serie = estado.historico[chave] || {
      nome: candidato.nomeUrna,
      cor: corPartido(candidato.partido),
      pontos: [],
    };
    serie.nome = candidato.nomeUrna;
    serie.cor = corPartido(candidato.partido);
    const ultimo = serie.pontos[serie.pontos.length - 1];
    if (!ultimo || ultimo[1] !== candidato.votos) {
      serie.pontos.push([agora, candidato.votos]);
      if (serie.pontos.length > 240) serie.pontos = serie.pontos.slice(-240);
    }
    estado.historico[chave] = serie;
  });

  try {
    localStorage.setItem(estado.chaveSerie, JSON.stringify(estado.historico));
  } catch { /* cota cheia: ignora */ }

  renderLinha();
}

function renderLinha() {
  const alvo = $('#graficoLinha');
  const dica = $('#hintLinha');
  const series = Object.entries(estado.historico)
    .map(([chave, serie]) => ({
      chave,
      nome: serie.nome,
      cor: serie.cor,
      pontos: serie.pontos,
      ultimo: serie.pontos[serie.pontos.length - 1]?.[1] || 0,
    }))
    .sort((a, b) => b.ultimo - a.ultimo)
    .slice(0, 4);

  const totalPontos = series.reduce((soma, s) => soma + s.pontos.length, 0);
  const temVotos = series.some((serie) => serie.ultimo > 0);
  if (series.length < 1 || totalPontos < 2 || !temVotos) {
    alvo.innerHTML = '';
    dica.textContent = temVotos
      ? 'O histórico começa a ser desenhado a partir da segunda atualização.'
      : 'A evolução aparecerá assim que os primeiros votos forem divulgados.';
    dica.hidden = false;
    return;
  }
  dica.hidden = true;

  const largura = 340;
  const altura = 150;
  const pad = { esq: 40, dir: 10, topo: 12, base: 24 };
  const larguraUtil = largura - pad.esq - pad.dir;
  const alturaUtil = altura - pad.topo - pad.base;

  const todosPontos = series.flatMap((s) => s.pontos);
  const tMin = Math.min(...todosPontos.map((p) => p[0]));
  const tMax = Math.max(...todosPontos.map((p) => p[0]));
  const vMax = Math.max(1, ...todosPontos.map((p) => p[1])) * 1.06;

  const x = (t) => pad.esq + ((t - tMin) / Math.max(1, tMax - tMin)) * larguraUtil;
  const y = (v) => pad.topo + (1 - v / vMax) * alturaUtil;

  const grades = [0, 0.5, 1]
    .map((fracao) => {
      const valor = vMax * fracao;
      const pos = y(valor);
      return `<line class="grade" x1="${pad.esq}" y1="${pos}" x2="${largura - pad.dir}" y2="${pos}" />
              <text class="eixo-txt" x="${pad.esq - 6}" y="${pos + 3}" text-anchor="end">${compacto(valor)}</text>`;
    })
    .join('');

  const linhas = series
    .map((serie) => {
      const pontos = serie.pontos
        .map(([t, v]) => `${x(t).toFixed(2)},${y(v).toFixed(2)}`)
        .join(' ');
      const ultimo = serie.pontos[serie.pontos.length - 1];
      return `
        <polyline points="${pontos}" stroke="${serie.cor}" />
        <circle class="ponto" cx="${x(ultimo[0]).toFixed(2)}" cy="${y(ultimo[1]).toFixed(2)}" r="3.2" fill="${serie.cor}" />`;
    })
    .join('');

  alvo.innerHTML = `
    <svg viewBox="0 0 ${largura} ${altura}" role="img" aria-label="Evolução de votos ao longo do tempo">
      ${grades}
      <line class="eixo" x1="${pad.esq}" y1="${altura - pad.base}" x2="${largura - pad.dir}" y2="${altura - pad.base}" />
      ${linhas}
      <text class="eixo-txt" x="${pad.esq}" y="${altura - 6}">${horaCurta(tMin)}</text>
      <text class="eixo-txt" x="${largura - pad.dir}" y="${altura - 6}" text-anchor="end">${horaCurta(tMax)}</text>
    </svg>
    <div class="legenda" style="margin-top:6px;flex-direction:row;flex-wrap:wrap;gap:10px">
      ${series
        .map(
          (serie) => `
        <span class="chip" style="border-color:color-mix(in srgb, ${serie.cor} 45%, transparent)">
          <span class="cor" style="width:8px;height:8px;border-radius:2px;background:${serie.cor}"></span>
          ${esc(serie.nome)}
        </span>`,
        )
        .join('')}
    </div>`;
}

/* --------------------------------------------------------- acompanhamento */

function renderAcompanhamento(dados) {
  const painel = $('#painelAcomp');
  const corpo = $('#tabelaAcomp');
  const linhas = dados.linhas || [];

  if (!linhas.length) {
    painel.hidden = true;
    return;
  }
  painel.hidden = false;

  const totalSecoes = linhas.reduce((soma, l) => soma + (l.secoes.total || 0), 0);
  const totalizadas = linhas.reduce((soma, l) => soma + (l.secoes.totalizadas || 0), 0);
  const estados = linhas.filter((l) => l.codigo !== 'zz').length;
  const temExterior = linhas.some((l) => l.codigo === 'zz');

  const ordenadas = [...linhas].sort((a, b) => a.nome.localeCompare(b.nome, 'pt-BR'));

  $('#chipsAcomp').innerHTML = `
    <span class="chip"><strong>${fmtInt.format(estados)}</strong> estados${temExterior ? ' + exterior' : ''}</span>
    <span class="chip"><strong>${fmtInt.format(totalizadas)}</strong> de ${fmtInt.format(totalSecoes)} seções</span>`;

  corpo.innerHTML = `
    <table class="tabela">
      <thead>
        <tr>
          <th>Estado</th>
          <th class="num">Seções apuradas</th>
          <th>Progresso</th>
          <th class="num">Comparecimento</th>
          <th class="num">Abstenção</th>
        </tr>
      </thead>
      <tbody>
        ${ordenadas
          .map(
            (linha) => `
          <tr>
            <td class="local">${esc(linha.nome)}</td>
            <td class="num">${pct(linha.secoes.pct)} <span style="color:var(--dim-2)">(${fmtInt.format(
              linha.secoes.totalizadas,
            )})</span></td>
            <td>
              <span class="mini">
                <span class="mini-barra"><i style="width:${Math.min(100, linha.secoes.pct)}%"></i></span>
              </span>
            </td>
            <td class="num">${pct(linha.comparecimento.pct)}</td>
            <td class="num">${pct(linha.abstencao.pct)}</td>
          </tr>`,
          )
          .join('')}
      </tbody>
    </table>`;
}

/* -------------------------------------------------------------------- mapa */

function urlMapa() {
  const params = new URLSearchParams({
    ciclo: estado.eleicao.ciclo,
    eleicao: estado.eleicao.cd,
    cargo: estado.cargo,
  });
  return `${API.mapa}?${params.toString()}`;
}

function corMapa(indice) {
  const candidato = mapa.candidatos[indice];
  return candidato ? corPartido(candidato.partido) : '#64789a';
}

function horaDeIso(iso) {
  if (!iso) return '';
  const data = new Date(iso);
  if (Number.isNaN(data.getTime())) return '';
  return data.toLocaleTimeString('pt-BR', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

async function iniciarMapa() {
  if (!estado.eleicao) return;
  if (mapa.malha) {
    if (!mapa.ocupado) carregarMapa();
    return;
  }
  if (mapa.iniciado) return;

  mapa.iniciado = true;
  $('#painelMapa').hidden = false;
  const status = $('#mapaStatus');
  status.hidden = false;
  status.textContent = 'Carregando os contornos dos municípios…';
  delete status.dataset.tipo;

  try {
    const malha = await getJSON(API.malha);
    construirMapa(malha);
    await carregarMapa();
  } catch (erro) {
    mapa.iniciado = false;
    status.hidden = false;
    status.dataset.tipo = 'erro';
    status.textContent = `Não foi possível carregar o mapa: ${erro.message}`;
  }
}

function construirMapa(malha) {
  if (!malha || !Array.isArray(malha.municipios) || !malha.municipios.length) {
    throw new Error('Malha municipal inválida.');
  }
  mapa.malha = malha;
  const svg = (mapa.svg = $('#mapaBrasil'));
  const largura = Number(malha.largura) || 8000;
  const altura = Number(malha.altura) || 7919;
  svg.setAttribute('viewBox', `0 0 ${largura} ${altura}`);
  svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  const partes = new Array(malha.municipios.length);
  for (let i = 0; i < malha.municipios.length; i += 1) {
    const [codigo, caminho] = malha.municipios[i];
    partes[i] = `<path class="mun" data-c="${codigo}" d="${caminho}"></path>`;
  }
  svg.innerHTML = `<g id="mapaCamada">${partes.join('')}</g>`;
  mapa.camada = svg.querySelector('#mapaCamada');
  mapa.paths = new Map();
  for (const elemento of mapa.camada.children) mapa.paths.set(elemento.dataset.c, elemento);

  mapa.view = { x: 0, y: 0, w: largura, h: altura };
  aplicarVistaMapa();
  configurarMapaInteracao();
  $('#mapaStatus').hidden = true;
}

function configurarMapaInteracao() {
  if (mapa.interacaoLigada || !mapa.svg) return;
  mapa.interacaoLigada = true;
  const svg = mapa.svg;
  svg.addEventListener('wheel', aoRolarMapa, { passive: false });
  svg.addEventListener('pointerdown', aoPressionarMapa);
  svg.addEventListener('pointermove', aoMoverMapa);
  svg.addEventListener('pointerup', aoSoltarMapa);
  svg.addEventListener('pointercancel', aoSoltarMapa);
  svg.addEventListener('pointerleave', () => {
    if (!mapa.arrasto) esconderDicaMapa();
  });
  document.addEventListener(
    'pointerdown',
    (evento) => {
      if (!mapa.svg || mapa.svg.contains(evento.target)) return;
      esconderDicaMapa();
    },
    true,
  );
  window.addEventListener('scroll', esconderDicaMapa, { passive: true });
}

function pontoSvg(evento) {
  const svg = mapa.svg;
  if (!svg) return { x: 0, y: 0 };
  const ponto = svg.createSVGPoint();
  ponto.x = evento.clientX;
  ponto.y = evento.clientY;
  const matriz = svg.getScreenCTM();
  return matriz ? ponto.matrixTransform(matriz.inverse()) : { x: 0, y: 0 };
}

function aplicarZoom(fator, ponto = null) {
  if (!mapa.malha || !mapa.view) return;
  const largura = mapa.malha.largura;
  const altura = mapa.malha.altura;
  const vista = mapa.view;
  const novaLargura = Math.min(largura, Math.max(90, vista.w / fator));
  const novaAltura = novaLargura * (altura / largura);
  const centroX = ponto ? ponto.x : vista.x + vista.w / 2;
  const centroY = ponto ? ponto.y : vista.y + vista.h / 2;
  const fracaoX = (centroX - vista.x) / vista.w;
  const fracaoY = (centroY - vista.y) / vista.h;

  vista.x = centroX - fracaoX * novaLargura;
  vista.y = centroY - fracaoY * novaAltura;
  vista.w = novaLargura;
  vista.h = novaAltura;
  limitarVistaMapa();
  aplicarVistaMapa();
  esconderDicaMapa();
}

function limitarVistaMapa() {
  if (!mapa.malha || !mapa.view) return;
  const { largura, altura } = mapa.malha;
  const vista = mapa.view;
  vista.w = Math.min(largura, Math.max(60, vista.w));
  vista.h = vista.w * (altura / largura);
  vista.x = Math.min(Math.max(0, vista.x), Math.max(0, largura - vista.w));
  vista.y = Math.min(Math.max(0, vista.y), Math.max(0, altura - vista.h));
}

function aplicarVistaMapa() {
  if (!mapa.svg || !mapa.view) return;
  const vista = mapa.view;
  mapa.svg.setAttribute('viewBox', `${vista.x} ${vista.y} ${vista.w} ${vista.h}`);
}

function resetarMapa() {
  if (!mapa.malha) return;
  mapa.view = { x: 0, y: 0, w: mapa.malha.largura, h: mapa.malha.altura };
  aplicarVistaMapa();
  esconderDicaMapa();
}

function aoRolarMapa(evento) {
  if (!mapa.malha) return;
  evento.preventDefault();
  aplicarZoom(evento.deltaY < 0 ? 1.25 : 0.8, pontoSvg(evento));
}

function aoPressionarMapa(evento) {
  if (!mapa.malha || !mapa.view) return;
  if (evento.pointerType === 'mouse' && evento.button !== 0) return;
  const matriz = mapa.svg.getScreenCTM();
  mapa.arrasto = {
    x: evento.clientX,
    y: evento.clientY,
    vx: mapa.view.x,
    vy: mapa.view.y,
    escalaX: matriz ? 1 / matriz.a : 1,
    escalaY: matriz ? 1 / matriz.d : 1,
    movido: false,
    pointerId: evento.pointerId,
    alvo: evento.target.closest?.('path.mun')?.dataset.c || null,
  };
  try {
    mapa.svg.setPointerCapture(evento.pointerId);
  } catch { /* alguns navegadores não permitem capturar */ }
  mapa.svg.classList.add('arrastando');
  esconderDicaMapa();
  evento.preventDefault();
}

function aoMoverMapa(evento) {
  if (!mapa.malha) return;

  if (mapa.arrasto) {
    const arrasto = mapa.arrasto;
    const dx = evento.clientX - arrasto.x;
    const dy = evento.clientY - arrasto.y;
    if (Math.hypot(dx, dy) > 4) arrasto.movido = true;
    if (arrasto.movido) {
      mapa.view.x = arrasto.vx - dx * arrasto.escalaX;
      mapa.view.y = arrasto.vy - dy * arrasto.escalaY;
      limitarVistaMapa();
      aplicarVistaMapa();
    }
    return;
  }

  const alvo = evento.target.closest?.('path.mun');
  if (alvo) mostrarDicaMapa(alvo.dataset.c, evento.clientX, evento.clientY);
  else esconderDicaMapa();
}

function aoSoltarMapa(evento) {
  if (!mapa.arrasto) return;
  const arrasto = mapa.arrasto;
  mapa.arrasto = null;
  mapa.svg.classList.remove('arrastando');
  try {
    mapa.svg.releasePointerCapture(arrasto.pointerId);
  } catch { /* ignora */ }

  if (!arrasto.movido && arrasto.alvo) {
    mostrarDicaMapa(arrasto.alvo, evento.clientX, evento.clientY);
  }
}

function mostrarDicaMapa(codigo, clientX, clientY) {
  const dica = $('#mapaDica');
  const mesmaCidade = mapa.dicaCodigo === codigo && !dica.hidden;

  if (!mesmaCidade) {
    const municipio = mapa.dados.get(codigo);
    const nome = municipio ? municipio.n : 'Município';
    const uf = municipio ? String(municipio.u || '').toUpperCase() : '';

    let html = `<div class="dica-topo"><b>${esc(nome)}</b><span>${esc(uf)}</span></div>`;
    if (!municipio) {
      html += '<div class="dica-vazio">Município ainda não consultado.</div>';
    } else {
      const total = (municipio.v || []).reduce((soma, valor) => soma + valor, 0);
      if (total <= 0) {
        html += '<div class="dica-vazio">Sem votos computados até agora.</div>';
      } else if (municipio.w < 0) {
        html += '<div class="dica-vazio">Sem maioria definida (empate).</div>';
      } else {
        const candidato = mapa.candidatos[municipio.w];
        const cor = corMapa(municipio.w);
        const votos = municipio.v[municipio.w] || 0;
        html += `
          <div class="dica-linha" style="--cor:${cor}">
            <span class="cor"></span>
            <b>${esc(candidato?.nome || '—')}</b>
            <span>${pct(municipio.p, 1)}</span>
          </div>
          <div class="dica-votos">${fmtInt.format(votos)} votos</div>`;
      }
      if (municipio.s !== undefined && municipio.s !== null) {
        const secoes = municipio.s > 0
          ? `${pct(municipio.s, 1)} das seções apuradas`
          : 'Seções ainda não transmitidas';
        html += `<div class="dica-rodape">${secoes}${municipio.f ? ' · totalizado' : ''}</div>`;
      }
    }

    dica.innerHTML = html;
    dica.hidden = false;
    mapa.dicaCodigo = codigo;
    mapa.dicaTamanho = { largura: dica.offsetWidth, altura: dica.offsetHeight };
  }

  const palco = $('#mapaPalco').getBoundingClientRect();
  let x = clientX - palco.left + 14;
  let y = clientY - palco.top + 14;
  const largura = mapa.dicaTamanho?.largura || dica.offsetWidth;
  const altura = mapa.dicaTamanho?.altura || dica.offsetHeight;
  if (x + largura > palco.width - 8) x = clientX - palco.left - largura - 14;
  if (y + altura > palco.height - 8) y = clientY - palco.top - altura - 14;
  dica.style.left = `${Math.max(8, x)}px`;
  dica.style.top = `${Math.max(8, y)}px`;
}

function esconderDicaMapa() {
  const dica = $('#mapaDica');
  if (dica) dica.hidden = true;
  mapa.dicaCodigo = null;
  mapa.dicaTamanho = null;
}

async function carregarMapa(opcoes = {}) {
  if (!estado.eleicao || mapa.ocupado) return;
  mapa.ocupado = true;
  try {
    const dados = await getJSON(urlMapa());
    mapa.falhas = 0;
    aplicarDadosMapa(dados);
    if (opcoes.manual) toast('Mapa atualizado');
  } catch (erro) {
    mapa.falhas += 1;
    const status = $('#mapaStatus');
    status.hidden = false;
    status.dataset.tipo = 'erro';
    status.textContent = mapa.dados.size
      ? 'Sem conexão com o servidor do mapa; exibindo a última leitura.'
      : `Não foi possível carregar o mapa: ${erro.message}`;
  } finally {
    mapa.ocupado = false;
    agendarMapa();
  }
}

function aplicarDadosMapa(dados) {
  const candidatos = dados.candidatos || [];
  const candidatosMudaram =
    candidatos.length !== mapa.candidatos.length ||
    candidatos.some((candidato, indice) => candidato.sqcand !== mapa.candidatos[indice]?.sqcand);

  mapa.candidatos = candidatos;
  mapa.cores = candidatos.map((_, indice) => corMapa(indice));
  mapa.progresso = dados.progresso || {};
  mapa.resumo = dados.resumo || {};
  mapa.atualizadoEm = mapa.progresso.atualizadoEm || mapa.atualizadoEm;

  for (const item of dados.municipios || []) {
    if (!Array.isArray(item) || item.length < 7) continue;
    const [c, u, n, v, w, p, f, s] = item;
    mapa.dados.set(c, { c, u, n, v: v || [], w, p, f: Boolean(f), s });
    const elemento = mapa.paths.get(c);
    if (!elemento) continue;
    const chaveCor = String(w);
    if (candidatosMudaram || elemento.dataset.cor !== chaveCor) {
      elemento.style.fill = w >= 0 && mapa.cores[w] ? mapa.cores[w] : '';
      elemento.dataset.cor = chaveCor;
    }
  }

  renderLegendaMapa();
  renderProgressoMapa();

  const status = $('#mapaStatus');
  const progresso = mapa.progresso;
  if (progresso.erro) {
    status.hidden = false;
    status.dataset.tipo = 'erro';
    status.textContent = progresso.erro;
  } else if (!mapa.dados.size && !progresso.prontos) {
    status.hidden = false;
    status.dataset.tipo = 'carregando';
    status.textContent = 'Consultando os municípios do TSE…';
  } else {
    status.hidden = true;
    delete status.dataset.tipo;
  }
}

function renderLegendaMapa() {
  const alvo = $('#mapaLegenda');
  const total = mapa.progresso?.total || mapa.dados.size || 0;
  const porCandidato = mapa.resumo?.porCandidato || [];
  const semDados = Math.max(0, total - (mapa.resumo?.comDados || 0));

  if (!mapa.candidatos.length) {
    alvo.innerHTML = '<p class="hint" style="margin:0">Aguardando os primeiros votos para montar a legenda.</p>';
    return;
  }

  const itens = mapa.candidatos.map((candidato, indice) => {
    const resumo = porCandidato[indice] || { municipios: 0, votos: 0 };
    const cor = corMapa(indice);
    const fatia = total ? (resumo.municipios / total) * 100 : 0;
    return `
      <div class="mapa-item" style="--cor:${cor}" title="${fmtInt.format(resumo.votos)} votos nos municípios já consultados">
        <span class="cor"></span>
        <div class="mapa-item-txt">
          <b>${esc(candidato.nome)}</b>
          <small>${esc(candidato.partido || '—')}</small>
        </div>
        <div class="mapa-item-num">
          <strong>${fmtInt.format(resumo.municipios)}</strong>
          <small>${pct(fatia, 1)} das cidades</small>
        </div>
      </div>`;
  });

  itens.push(`
    <div class="mapa-item mapa-item-vazio" title="Cidades sem votos computados ou ainda não consultadas">
      <span class="cor"></span>
      <div class="mapa-item-txt"><b>Sem dados</b><small>aguardando votos</small></div>
      <div class="mapa-item-num">
        <strong>${fmtInt.format(semDados)}</strong>
        <small>${pct(total ? (semDados / total) * 100 : 0, 1)} das cidades</small>
      </div>
    </div>`);

  alvo.innerHTML = `<p class="mapa-titulo">Cidades por candidato</p>${itens.join('')}`;
}

function renderProgressoMapa() {
  const alvo = $('#mapaProgresso');
  const progresso = mapa.progresso || {};
  const total = progresso.total || 0;
  const prontos = progresso.prontos || 0;
  const percentual = total ? Math.min(100, (prontos / total) * 100) : 0;

  const partes = [];
  partes.push(
    `<div class="mapa-barra"><i style="width:${percentual.toFixed(1)}%"></i></div>`,
  );
  partes.push(
    `<p class="mapa-contagem"><strong>${fmtInt.format(prontos)}</strong> de ${fmtInt.format(
      total,
    )} municípios consultados</p>`,
  );

  if (progresso.erro) {
    partes.push(`<p class="mapa-nota erro">${esc(progresso.erro)}</p>`);
  } else if (!total) {
    partes.push('<p class="mapa-nota">Preparando a varredura dos municípios…</p>');
  } else if (progresso.varrendo) {
    const rotulo = total && prontos >= total ? 'Atualizando os municípios' : 'Varrendo o TSE';
    partes.push(
      `<p class="mapa-nota"><span class="pulso-mini"></span> ${rotulo} · onda ${fmtInt.format(
        progresso.onda || 1,
      )}</p>`,
    );
  } else if (progresso.finalizado) {
    partes.push('<p class="mapa-nota">Mapa completo · apuração totalizada</p>');
  } else if (progresso.nacional?.situacao === 'aguardando') {
    partes.push('<p class="mapa-nota">Aguardando a divulgação dos votos.</p>');
  } else {
    partes.push('<p class="mapa-nota">Atualização automática por ondas.</p>');
  }

  if (progresso.falhas) {
    partes.push(
      `<p class="mapa-nota">${fmtInt.format(progresso.falhas)} consulta(s) falharam nesta onda; tentaremos novamente.</p>`,
    );
  }

  const chips = [];
  if (total) chips.push(`<span class="chip"><strong>${fmtInt.format(total)}</strong> municípios</span>`);
  if (progresso.varrendo) {
    const rotulo = total && prontos >= total ? 'Atualizando' : `Carregando ${pct(percentual, 0)}`;
    chips.push(`<span class="chip" data-tone="warn">${rotulo}</span>`);
  } else if (progresso.finalizado) {
    chips.push('<span class="chip" data-tone="ok">Mapa completo</span>');
  } else if (progresso.nacional?.situacao === 'em_andamento') {
    chips.push('<span class="chip" data-tone="warn">Ao vivo</span>');
  }
  if (mapa.atualizadoEm) chips.push(`<span class="chip">${horaDeIso(mapa.atualizadoEm)}</span>`);
  $('#chipsMapa').innerHTML = chips.join('');

  alvo.innerHTML = partes.join('');
}

function agendarMapa() {
  clearTimeout(mapa.timer);
  if (!mapa.iniciado) return;
  const progresso = mapa.progresso || {};
  const total = progresso.total || 0;
  const prontos = progresso.prontos || 0;
  let atraso = 60000;
  if (!total || !progresso.nacional || !prontos) atraso = 3000;
  else if (progresso.varrendo || prontos < total) atraso = 6000;
  else if (progresso.nacional?.situacao === 'em_andamento') atraso = 20000;

  mapa.timer = setTimeout(() => {
    if (document.hidden) agendarMapa();
    else carregarMapa();
  }, atraso);
}

/* --------------------------------------------------------------- orquestra */

function ligarEventos() {
  const selPoll = $('#selPoll');
  let gestoNoIntervalo = false;
  ['pointerdown', 'keydown', 'touchstart'].forEach((tipo) => {
    selPoll.addEventListener(
      tipo,
      () => {
        gestoNoIntervalo = true;
        setTimeout(() => (gestoNoIntervalo = false), 4000);
      },
      { passive: true },
    );
  });

  selPoll.addEventListener('change', (evento) => {
    // O Chrome restaura o valor do <select> ao recarregar e às vezes dispara
    // "change" sozinho; só aceitamos mudanças vindas de um gesto real.
    if (!gestoNoIntervalo) {
      selPoll.value = String(estado.intervalo);
      return;
    }
    gestoNoIntervalo = false;
    estado.intervalo = Number(evento.target.value);
    gravarIntervalo();
    agendar();
    setLive(estado.intervalo ? 'on' : 'off', estado.intervalo ? 'Ao vivo' : 'Pausado');
    toast(estado.intervalo ? `Atualizando a cada ${estado.intervalo / 1000}s` : 'Atualização automática pausada');
  });

  $('#btnAtualizar').addEventListener('click', () => {
    carregar({ manual: true });
    if (mapa.malha) carregarMapa({ manual: true });
    else iniciarMapa();
  });
  $('#btnBanner').addEventListener('click', () => carregar({ manual: true }));
  $('#btnTema').addEventListener('click', alternarTema);

  $('#btnMapaMais').addEventListener('click', () => aplicarZoom(1.5));
  $('#btnMapaMenos').addEventListener('click', () => aplicarZoom(0.67));
  $('#btnMapaReset').addEventListener('click', resetarMapa);

  document.addEventListener('visibilitychange', () => {
    if (document.hidden) return;
    if (estado.intervalo) carregar();
    if (mapa.iniciado) carregarMapa();
  });

  window.painelAtualizar = () => carregar({ manual: true });
}

/* -------------------------------------------------------------------- boot */

function prepararEleicao() {
  const eleicao = estado.config?.eleicoes?.[0];
  if (!eleicao) {
    throw new Error('O TSE não publicou a eleição de presidente de 2026 (2º turno).');
  }
  estado.eleicao = eleicao;

  const cargos = eleicao.abrangencias.flatMap((abrangencia) => abrangencia.cargos);
  const presidente = cargos.find((cargo) => cargo.cd === '1') || cargos[0];
  estado.cargo = presidente?.cd || '1';

  carregarSerie();
}

async function iniciar() {
  try {
    const tema = localStorage.getItem('tse:tema');
    if (tema) document.documentElement.dataset.theme = tema;
    localStorage.removeItem('tse:prefs'); // formato antigo, com seleção de pleito
  } catch { /* ignora */ }

  ligarEventos();
  skeleton(true);

  estado.intervalo = lerIntervalo();
  $('#selPoll').value = String(estado.intervalo);

  try {
    estado.config = await getJSON(API.config);
    prepararEleicao();
    await carregar();
    carregarAcompanhamento();
    iniciarMapa();
  } catch (erro) {
    mostrarBanner(`Não foi possível carregar os dados do TSE: ${erro.message}`);
    setLive('erro');
    $('#heroCargo').textContent = 'Painel indisponível';
    $('#heroStatus').textContent = 'Verifique sua conexão e tente novamente.';
  } finally {
    skeleton(false);
  }
}

document.addEventListener('DOMContentLoaded', iniciar);
