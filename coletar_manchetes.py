#!/usr/bin/env python3
"""
Coletor de manchetes / detector de furos.

1. Abre as páginas de seção configuradas em config.yaml (Playwright/Chromium).
2. Guarda os links que batem com o padrão de matéria de cada veículo.
3. Para os links novos, lê título, linha fina e horário de publicação (meta tags).
4. Pontua cada manchete com as regras de config.yaml.
5. Acrescenta as linhas novas na aba "Manchetes" da planilha do Google
   (ou só gera um CSV, no modo --testar).

Variáveis de ambiente (no GitHub, cadastradas como secrets):
  GOOGLE_SERVICE_ACCOUNT_JSON  conteúdo do arquivo JSON da conta de serviço
  PLANILHA_ID                  ID da planilha (o trecho da URL entre /d/ e /edit)
"""

import argparse
import asyncio
import csv
import html
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import yaml

TZ = ZoneInfo("America/Sao_Paulo")
ABA = "Manchetes"
CABECALHO = ["Coletado em", "Publicado em", "Veículo", "Seção/coluna", "Manchete",
             "Linha fina", "Link", "Pontuação", "Sinais", "Candidato"]
COL_LINK = CABECALHO.index("Link") + 1  # coluna G
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- texto e links

def normalizar(texto):
    """Minúsculas, sem acento, espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def limpar_link(url):
    """Tira parâmetros, âncoras e versões AMP para o mesmo artigo virar um link só."""
    partes = urlsplit(url.strip())
    caminho = partes.path.replace("/google/amp/", "/")
    return urlunsplit(("https", partes.netloc.lower(), caminho, "", ""))


def secao_do_link(url):
    """Ex.: .../colunas/painel/2026/11/x.shtml -> colunas/painel"""
    partes = [p for p in urlsplit(url).path.split("/") if p]
    secao = []
    for p in partes[:-1]:
        if re.fullmatch(r"\d{4}", p) or p in ("noticia", "post"):
            break
        secao.append(p)
    return "/".join(secao)


# ---------------------------------------------------------------- pontuação

def compilar_sinais(cfg_pontuacao):
    sinais = []
    for s in cfg_pontuacao["sinais"]:
        termos = [normalizar(t) for t in s.get("termos", [])]
        padrao = None
        if termos:
            alternativas = "|".join(re.escape(t) for t in sorted(termos, key=len, reverse=True))
            padrao = re.compile(rf"(?<!\w)(?:{alternativas})(?!\w)")
        sinais.append({
            "nome": s["nome"],
            "peso": s["peso"],
            "padrao": padrao,
            "regex": re.compile(s["regex"]) if s.get("regex") else None,
            "trechos_link": [t.lower() for t in s.get("trechos_link", [])],
        })
    return sinais


def pontuar(manchete, linha_fina, link, sinais):
    texto = normalizar(f"{manchete} {linha_fina}")
    link = link.lower()
    total, achados = 0, []
    for s in sinais:
        bateu = (
            (s["padrao"] and s["padrao"].search(texto))
            or (s["regex"] and s["regex"].search(texto))
            or any(t in link for t in s["trechos_link"])
        )
        if bateu:
            total += s["peso"]
            achados.append(s["nome"])
    return total, achados


# ---------------------------------------------------------------- coleta

JS_LINKS = """els => els.map(e => ({
  href: e.href,
  texto: (e.innerText || e.getAttribute('aria-label') || e.getAttribute('title') || '').trim()
}))"""


def texto_da_ancora(texto):
    """Cards costumam ter chapéu + título + linha fina; fica a linha mais longa."""
    linhas = [l.strip() for l in texto.splitlines() if l.strip()]
    return max(linhas, key=len) if linhas else ""


async def coletar_pagina(context, fonte, url, excluir):
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        await page.wait_for_timeout(2_500)
        for _ in range(3):  # rola para carregar blocos preguiçosos
            await page.mouse.wheel(0, 3_000)
            await page.wait_for_timeout(700)
        ancoras = await page.eval_on_selector_all("a[href]", JS_LINKS)
    except Exception as erro:
        log(f"  ! falha em {url}: {type(erro).__name__}: {str(erro)[:150]}")
        return []
    finally:
        await page.close()

    padrao = re.compile(fonte["padrao_link"])
    achados = {}
    for a in ancoras:
        if not a["href"].startswith("http"):
            continue
        link = limpar_link(a["href"])
        if not padrao.search(link) or any(x in link for x in excluir):
            continue
        texto = texto_da_ancora(a["texto"])
        if link not in achados or len(texto) > len(achados[link]):
            achados[link] = texto
    log(f"  {fonte['veiculo']:<8} {len(achados):>3} links  {url}")
    return [{"veiculo": fonte["veiculo"], "link": l, "manchete": t} for l, t in achados.items()]


RE_META = re.compile(r"<meta\s[^>]*>", re.I)
RE_ATTR = re.compile(r'([\w:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')
RE_JSONLD_DATA = re.compile(r'"datePublished"\s*:\s*"([^"]+)"')


def ler_metas(pagina_html):
    metas = {}
    for tag in RE_META.findall(pagina_html):
        attrs = {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4))
                 for m in RE_ATTR.finditer(tag)}
        chave = attrs.get("property") or attrs.get("name") or attrs.get("itemprop")
        if chave and "content" in attrs and chave.lower() not in metas:
            metas[chave.lower()] = html.unescape(attrs["content"]).strip()
    return metas


def ler_data(valor):
    try:
        dt = datetime.fromisoformat(valor.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


RE_DATA_LINK = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/")


def dia_do_link(url):
    """Globo e Valor trazem o dia na URL (/2026/11/05/). Folha só traz o mês."""
    m = RE_DATA_LINK.search(url)
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=TZ).date()
    except ValueError:
        return None


async def completar_artigo(context, item, sem):
    """Lê título oficial, linha fina e horário de publicação da página do artigo."""
    async with sem:
        try:
            resp = await context.request.get(item["link"], timeout=30_000)
            if not resp.ok:
                return
            pagina = await resp.text()
        except Exception:
            return
    metas = ler_metas(pagina)
    titulo = metas.get("og:title") or metas.get("twitter:title")
    if titulo:
        item["manchete"] = titulo
    item["linha_fina"] = metas.get("og:description") or metas.get("description") or ""
    data = (metas.get("article:published_time") or metas.get("datepublished")
            or (RE_JSONLD_DATA.search(pagina) or [None, ""])[1])
    dt = ler_data(data) if data else None
    item["publicado_dt"] = dt
    item["publicado"] = dt.strftime("%Y-%m-%d %H:%M") if dt else ""


async def coletar(cfg, ja_vistos):
    from playwright.async_api import async_playwright

    excluir = cfg.get("excluir_links", [])
    async with async_playwright() as pw:
        navegador = await pw.chromium.launch()
        context = await navegador.new_context(
            user_agent=USER_AGENT, locale="pt-BR", timezone_id="America/Sao_Paulo")

        brutos = []
        for fonte in cfg["fontes"]:
            for url in fonte["paginas"]:
                brutos += await coletar_pagina(context, fonte, url, excluir)

        janela = timedelta(hours=cfg.get("janela_horas", 24))
        corte = datetime.now(TZ) - janela
        novos, vistos_agora, velhos_pela_url = [], set(), 0
        for item in brutos:
            if item["link"] in ja_vistos or item["link"] in vistos_agora:
                continue
            vistos_agora.add(item["link"])
            dia = dia_do_link(item["link"])
            if dia and dia < corte.date():  # descarta sem nem abrir a matéria
                velhos_pela_url += 1
                continue
            item.update(linha_fina="", publicado="", publicado_dt=None)
            novos.append(item)

        limite = cfg.get("max_artigos_por_execucao", 300)
        log(f"{len(novos)} links novos ({velhos_pela_url} descartados pela data na URL); "
            f"lendo detalhes de até {limite}.")
        sem = asyncio.Semaphore(8)
        await asyncio.gather(*(completar_artigo(context, i, sem) for i in novos[:limite]))
        await navegador.close()

    recentes = [i for i in novos if not i["publicado_dt"] or i["publicado_dt"] >= corte]
    sem_data = sum(1 for i in recentes if not i["publicado_dt"])
    log(f"{len(novos) - len(recentes)} fora da janela de {janela.total_seconds() / 3600:.0f}h descartados; "
        f"{len(recentes)} mantidos ({sem_data} sem horário de publicação).")
    return recentes


# ---------------------------------------------------------------- planilha

def abrir_aba():
    import gspread

    credenciais = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    planilha = gspread.service_account_from_dict(credenciais).open_by_key(os.environ["PLANILHA_ID"])
    try:
        aba = planilha.worksheet(ABA)
    except gspread.WorksheetNotFound:
        aba = planilha.add_worksheet(ABA, rows=1000, cols=len(CABECALHO))
    if aba.row_values(1) != CABECALHO:
        aba.update([CABECALHO], "A1")
        aba.freeze(rows=1)
    return aba


def montar_linhas(itens, cfg):
    sinais = compilar_sinais(cfg["pontuacao"])
    minimo = cfg["pontuacao"]["minimo_candidato"]
    agora = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    linhas = []
    for i in itens:
        pontos, achados = pontuar(i["manchete"], i["linha_fina"], i["link"], sinais)
        linhas.append([agora, i["publicado"], i["veiculo"], secao_do_link(i["link"]),
                       i["manchete"], i["linha_fina"], i["link"], pontos,
                       ", ".join(achados), "sim" if pontos >= minimo else ""])
    linhas.sort(key=lambda l: (-l[7], l[2]))
    return linhas


def salvar_csv(linhas, caminho):
    with open(caminho, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CABECALHO)
        w.writerows(linhas)


# ---------------------------------------------------------------- principal

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--csv", default="coleta.csv", help="cópia da execução em CSV")
    ap.add_argument("--testar", action="store_true", help="não grava na planilha")
    args = ap.parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    aba, ja_vistos = None, set()
    if not args.testar:
        aba = abrir_aba()
        ja_vistos = set(aba.col_values(COL_LINK)[1:])
        log(f"{len(ja_vistos)} links já estavam na planilha.")

    itens = asyncio.run(coletar(cfg, ja_vistos))
    linhas = montar_linhas(itens, cfg)
    salvar_csv(linhas, args.csv)

    candidatos = [l for l in linhas if l[9]]
    log(f"\n{len(linhas)} manchetes novas, {len(candidatos)} candidatas a furo.")
    for l in candidatos[:15]:
        log(f"  [{l[7]:>2}] {l[2]}: {l[4]}  ({l[8]})")

    if aba and linhas:
        aba.append_rows(linhas, value_input_option="RAW")
        log("Planilha atualizada.")
    if not linhas and not args.testar:
        log("Nada novo para gravar.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
