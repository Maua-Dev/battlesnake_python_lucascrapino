# Bem-vindo ao
# __________         __    __  .__                               __
# \______   \_____ _/  |__/  |_|  |   ____   ______ ____ _____  |  | __ ____
#  |    |  _/\__  \   __\   __\  | _/ __ \ /  ___//    \__  \ |  |/ // __ \
#  |    |   \ / __ \|  |  |  | |  |_\  ___/ \___ \|   |  \/ __ \|    <\  ___/
#  |________/(______/__|  |__| |____/\_____>______>___|__(______/__|__\_____>
#
#

import logging
import time
from collections import deque

from .models import GameState, MoveResponse

logger = logging.getLogger(__name__)
# O runtime Python da Lambda deixa o logger raiz em WARNING: sem esta linha
# as jogadas nao aparecem no CloudWatch.
logger.setLevel(logging.INFO)

MOVES = ("up", "down", "left", "right")
DX = (0, 0, -1, 1)
DY = (1, -1, 0, 0)

# Pontuações-base. Valores grandes = "praticamente proibido".
MORTE_CERTA = -1_000_000.0
CABECA_PERDEDORA = -5_000.0
ARMADILHA = -3_000.0

# Busca do mano a mano.
VITORIA = 1_000_000.0
EMPATE = -500_000.0  # morrer junto ainda é não ganhar

# Tempo de pensamento. O Battlesnake dá `game.timeout` ms por jogada (500 no
# padrão), mas isso inclui a ida e volta pela internet. Usamos uma fração.
FRACAO_DO_TIMEOUT = 0.40
TETO_MS = 150.0


class SemTempo(Exception):
    """Estourou o prazo da busca: fica com o resultado da profundidade anterior."""


def info() -> dict:
    """GET / — chamado quando você cadastra a cobra e a cada partida.
    Opções de cabeça, cauda e cor: https://docs.battlesnake.com/guides/customizations
    """
    logger.info("INFO")

    return {
        "apiversion": "1",
        "author": "",          # TODO: coloque aqui o SEU usuário do Battlesnake
        "color": "#2E8B57",
        "head": "sneaky",
        "tail": "skinny",
        "version": "2.0.0",
    }


def start(state: GameState) -> None:
    """POST /start — chamado uma vez, quando a partida começa."""
    logger.info("JOGO COMEÇOU (partida %s)", state.game.id)


def end(state: GameState) -> None:
    """POST /end — chamado uma vez, quando a partida termina."""
    logger.info("FIM DE JOGO após %d turnos", state.turn)


# ---------------------------------------------------------------------------
# Leitura do tabuleiro (casas viram inteiros: y * largura + x)
# ---------------------------------------------------------------------------

class Tabuleiro:
    def __init__(self, state: GameState, prazo: float):
        self.prazo = prazo
        self.w = w = state.board.width
        self.h = h = state.board.height
        self.n = w * h
        self.me_id = state.you.id

        # Vizinhos pré-calculados de cada casa: lista de (direção, casa).
        self.viz = []
        for c in range(self.n):
            x, y = c % w, c // w
            lst = []
            for d in range(4):
                nx, ny = x + DX[d], y + DY[d]
                if 0 <= nx < w and 0 <= ny < h:
                    lst.append((d, ny * w + nx))
            self.viz.append(lst)

        self.my_body = self.converter(state.you.body)
        self.my_len = len(state.you.body)
        self.health = state.you.health
        self.food = {self.idx(c.x, c.y) for c in state.board.food if self.dentro(c.x, c.y)}
        self.hazards = {self.idx(c.x, c.y) for c in state.board.hazards if self.dentro(c.x, c.y)}

        settings = {}
        if isinstance(state.game.ruleset, dict):
            settings = state.game.ruleset.get("settings") or {}
        self.hazard_damage = int(settings.get("hazardDamagePerTurn", 14) or 0)

        self.opps = []
        for s in state.board.snakes:
            if s.id == self.me_id or not s.body:
                continue
            body = self.converter(s.body)
            if not body:
                continue
            self.opps.append({"id": s.id, "body": body, "head": body[0],
                              "len": len(s.body), "health": s.health})

        # occ[casa] = em quantos turnos (a partir de agora) a casa fica livre.
        # Segmento i de uma cobra com L partes some depois de L - i movimentos.
        self.occ_opps = [0] * self.n
        for o in self.opps:
            marcar_corpo(self.occ_opps, o["body"], o["len"], 0)

    def dentro(self, x, y):
        return 0 <= x < self.w and 0 <= y < self.h

    def idx(self, x, y):
        return y * self.w + x

    def converter(self, coords):
        # Segmentos fora do tabuleiro (só acontece em testes) viram -1.
        return [self.idx(c.x, c.y) if self.dentro(c.x, c.y) else -1 for c in coords]

    def manhattan(self, a, b):
        return abs(a % self.w - b % self.w) + abs(a // self.w - b // self.w)

    def borda(self, c):
        """Distância até a parede mais próxima (0 = encostado na parede)."""
        x, y = c % self.w, c // self.w
        return min(x, y, self.w - 1 - x, self.h - 1 - y)

    def destino(self, c, d):
        """Casa vizinha na direção d, ou -1 se sair do tabuleiro."""
        x, y = c % self.w + DX[d], c // self.w + DY[d]
        if 0 <= x < self.w and 0 <= y < self.h:
            return y * self.w + x
        return -1

    def checar_tempo(self):
        if time.perf_counter() > self.prazo:
            raise SemTempo()


def marcar_corpo(occ, body, n, offset):
    for i, seg in enumerate(body):
        if seg < 0:
            continue
        livre_em = offset + n - i
        # Cauda empilhada (acabou de comer) aparece repetida: vale o menor índice.
        if occ[seg] < livre_em:
            occ[seg] = livre_em


# ---------------------------------------------------------------------------
# Buscas no tabuleiro
# ---------------------------------------------------------------------------

def flood_fill(tab, origem, occ, t0):
    """BFS com noção de tempo. Uma casa só pode ser pisada no turno t se
    occ[casa] <= t. Devolve (área alcançável, distâncias; -1 = inalcançável)."""
    dist = [-1] * tab.n
    dist[origem] = 0
    fila = deque([origem])
    area = 1
    viz = tab.viz
    while fila:
        p = fila.popleft()
        prox = dist[p] + 1
        for _, q in viz[p]:
            if dist[q] != -1 or occ[q] > t0 + prox:
                continue
            dist[q] = prox
            area += 1
            fila.append(q)
    return area, dist


def sobrevivencia(tab, corpo, alvo, orcamento):
    """Quantos turnos a cobra aguenta viva a partir de `corpo` (cabeça já na
    casa nova, turno 1), simulando o próprio corpo andando passo a passo.
    O flood fill sozinho é otimista: conta casas que só liberam no futuro,
    mesmo quando não dá para esperar até lá."""
    melhor = 0
    nos = 0
    occ = tab.occ_opps
    viz = tab.viz
    prazo = tab.prazo

    def dfs(body, t, prof):
        nonlocal melhor, nos
        if prof > melhor:
            melhor = prof
        if melhor >= alvo or nos >= orcamento:
            return
        nos += 1
        if nos % 256 == 0 and time.perf_counter() > prazo:
            nos = orcamento
            return
        resto = body[:-1]  # a cauda sai do lugar neste passo
        for _, q in viz[body[0]]:
            if occ[q] > t + 1 or q in resto:
                continue
            dfs((q,) + resto, t + 1, prof + 1)
            if melhor >= alvo or nos >= orcamento:
                return

    dfs(tuple(c for c in corpo if c >= 0), 1, 0)
    return melhor


def voronoi(tab, fontes, tamanhos, occ):
    """Cada casa fica com a cobra que chega primeiro. `fontes` é uma lista de
    (casa, turno de partida, dono). Empate: fica com a maior; tamanhos iguais
    = ninguém. Devolve (dono por casa, turno de chegada por casa)."""
    n = tab.n
    dono = [-1] * n
    tempo = [10 ** 9] * n
    fronteira = []
    pendentes = sorted(fontes, key=lambda f: f[1])
    t = pendentes[0][1] if pendentes else 0
    i = 0
    viz = tab.viz
    while fronteira or i < len(pendentes):
        chegadas = {}
        while i < len(pendentes) and pendentes[i][1] == t:
            c, _, quem = pendentes[i]
            chegadas.setdefault(c, set()).add(quem)
            i += 1
        for p, quem in fronteira:
            for _, q in viz[p]:
                if tempo[q] < t or occ[q] > t:
                    continue
                chegadas.setdefault(q, set()).add(quem)
        fronteira = []
        for q, quems in chegadas.items():
            if tempo[q] < t:
                continue
            tempo[q] = t
            if len(quems) == 1:
                quem = next(iter(quems))
            else:
                ordem = sorted(quems, key=lambda k: tamanhos[k], reverse=True)
                quem = ordem[0] if tamanhos[ordem[0]] > tamanhos[ordem[1]] else -1
            dono[q] = quem
            if quem != -1:
                fronteira.append((q, quem))
        t += 1
        if t > n + 2:
            break
    return dono, tempo


# ---------------------------------------------------------------------------
# Fase 1: várias cobras vivas -> heurística de uma jogada
# ---------------------------------------------------------------------------

def avaliar(tab, d):
    head = tab.my_body[0]
    nova = tab.destino(head, d)

    # 1. Morte certa: parede, corpo (a cauda que vai sair é liberada), fome.
    if nova < 0:
        return MORTE_CERTA
    occ_agora = tab.occ_opps[:]
    marcar_corpo(occ_agora, tab.my_body, tab.my_len, 0)
    if occ_agora[nova] > 1:
        return MORTE_CERTA

    comeu = nova in tab.food
    vida = 100 if comeu else tab.health - 1
    if nova in tab.hazards and not comeu:
        vida -= tab.hazard_damage
    if vida <= 0:
        return MORTE_CERTA

    novo_corpo = [nova] + (tab.my_body if comeu else tab.my_body[:-1])
    meu_len = len(novo_corpo)
    score = 0.0

    # 2. Cabeça com cabeça: fase de sobrevivência, então só arrisca contra menores.
    for o in tab.opps:
        if tab.manhattan(o["head"], nova) == 1:
            score += CABECA_PERDEDORA if o["len"] >= tab.my_len else 10.0

    # 3. Espaço: nada de beco sem saída.
    occ = tab.occ_opps[:]
    marcar_corpo(occ, novo_corpo, meu_len, 1)
    occ[nova] = 0
    area, dist = flood_fill(tab, nova, occ, 1)
    alvo = min(meu_len + 2, 30)
    folego = sobrevivencia(tab, novo_corpo, alvo, 700)
    if folego < alvo:
        score += ARMADILHA + folego * 80.0
    elif area < meu_len:
        score += -400.0 + area * 10.0
    else:
        score += min(area, meu_len * 3) * 1.0

    # 4. Território: eu parto da casa nova (turno 1), os rivais das cabeças (turno 0).
    fontes = [(nova, 1, 0)]
    tamanhos = {0: tab.my_len}
    for k, o in enumerate(tab.opps, start=1):
        fontes.append((o["head"], 0, k))
        tamanhos[k] = o["len"]
    occ_v = occ[:]
    for o in tab.opps:
        occ_v[o["head"]] = 0
    dono, _ = voronoi(tab, fontes, tamanhos, occ_v)

    # Fome dinâmica: considera quão longe está a comida alcançável mais próxima.
    mais_perto = min((dist[f] for f in tab.food if dist[f] >= 0), default=None)
    fome = tab.health < 35 or (mais_perto is not None and tab.health <= mais_perto + 15)
    if mais_perto is not None and tab.health <= mais_perto + 6:
        # Fome crítica: chegar na comida vem antes de qualquer outra coisa.
        score -= 60.0 * mais_perto

    score += (0.3 if fome else 1.0) * sum(1 for v in dono if v == 0)

    # 5. Fugir da briga: longe das cabeças, bem mais longe das que podem me matar.
    for o in tab.opps:
        dd = tab.manhattan(o["head"], nova)
        if dd <= 4:
            score -= (5 - dd) * (35.0 if o["len"] >= tab.my_len else 12.0)

    # 6. Andar pelas beiradas (com fome, a comida vem antes).
    if not fome:
        score += {0: 22.0, 1: 16.0, 2: 6.0}.get(tab.borda(nova), 0.0)

    # 7. Comida: prioridade para as que ficam perto das bordas e cantos.
    maior_rival = max((o["len"] for o in tab.opps), default=0)
    vontade = 2.5 if fome else (1.3 if tab.my_len <= maior_rival + 3 else 0.7)
    melhor = 0.0
    for f in tab.food:
        df = dist[f]
        if df < 0:
            continue
        minha = dono[f] == 0 or f == nova
        seguro = 1.0 if (minha or dono[f] == -1 or fome) else 0.15
        local = 1.0 if fome else {0: 1.0, 1: 0.9, 2: 0.5}.get(tab.borda(f), 0.2)
        if tab.my_len < maior_rival:
            local = max(local, 0.6)  # menor que o maior rival: crescer vem antes
        if fome and tab.health <= df + 2:
            local = 2.0  # ou come agora, ou morre
        valor = 80.0 * local * seguro / (df + 1)
        if fome:
            # Com fome, a distância pesa de forma linear: comida longe também conta.
            valor = max(valor, (300.0 - 10.0 * df) * seguro)
        melhor = max(melhor, valor)
    score += vontade * melhor

    # 8. Hazard tira vida: evita quando der.
    if nova in tab.hazards and not comeu:
        score -= 40.0 + (100 - tab.health)

    return score


# ---------------------------------------------------------------------------
# Fase 2: mano a mano -> minimax com alpha-beta
# ---------------------------------------------------------------------------

def simular(tab, a, b, food, da, db):
    """Aplica as jogadas simultâneas seguindo a ordem oficial de resolução.
    a e b são (corpo, vida). Devolve (novo_a, novo_b, comida, a_vivo, b_vivo)."""
    corpo_a, vida_a = a
    corpo_b, vida_b = b
    ha = tab.destino(corpo_a[0], da)
    hb = tab.destino(corpo_b[0], db)

    vivo_a = ha >= 0
    vivo_b = hb >= 0
    if vivo_a:
        corpo_a = (ha,) + corpo_a[:-1]
        vida_a -= 1
        if ha in tab.hazards and ha not in food:
            vida_a -= tab.hazard_damage
    if vivo_b:
        corpo_b = (hb,) + corpo_b[:-1]
        vida_b -= 1
        if hb in tab.hazards and hb not in food:
            vida_b -= tab.hazard_damage

    nova_food = food
    if vivo_a and ha in food:
        vida_a = 100
        corpo_a = corpo_a + (corpo_a[-1],)
    if vivo_b and hb in food:
        vida_b = 100
        corpo_b = corpo_b + (corpo_b[-1],)
    if (vivo_a and ha in food) or (vivo_b and hb in food):
        nova_food = food - {ha, hb}

    if vivo_a and (vida_a <= 0 or ha in corpo_a[1:] or (vivo_b and ha in corpo_b[1:])):
        vivo_a = False
    if vivo_b and (vida_b <= 0 or hb in corpo_b[1:] or (ha >= 0 and hb in corpo_a[1:])):
        vivo_b = False
    if ha >= 0 and ha == hb:
        if len(corpo_a) > len(corpo_b):
            vivo_b = False
        elif len(corpo_b) > len(corpo_a):
            vivo_a = False
        else:
            vivo_a = vivo_b = False
    return (corpo_a, vida_a), (corpo_b, vida_b), nova_food, vivo_a, vivo_b


def jogadas_plausiveis(tab, corpo):
    """Direções que não batem na parede nem no pescoço."""
    pescoco = corpo[1] if len(corpo) > 1 else -1
    res = [d for d in range(4) if (q := tab.destino(corpo[0], d)) >= 0 and q != pescoco]
    return res or [0]


def avaliar_folha(tab, a, b, food):
    corpo_a, vida_a = a
    corpo_b, vida_b = b
    la, lb = len(corpo_a), len(corpo_b)
    occ = [0] * tab.n
    marcar_corpo(occ, corpo_a, la, 0)
    marcar_corpo(occ, corpo_b, lb, 0)
    occ[corpo_a[0]] = 0
    occ[corpo_b[0]] = 0
    dono, tempo = voronoi(tab, [(corpo_a[0], 0, 0), (corpo_b[0], 0, 1)], {0: la, 1: lb}, occ)
    meu = dono.count(0)
    dele = dono.count(1)

    score = 4.0 * (meu - dele)
    score += 25.0 * (la - lb)
    if meu < la:
        score -= 3000.0 - 40.0 * meu
    if dele < lb:
        score += 3000.0 - 40.0 * dele

    # Comida mais próxima que é minha: importa mais se estou menor ou com fome.
    perto = min((tempo[f] for f in food if dono[f] == 0), default=None)
    qualquer = min((tempo[f] for f in food if tempo[f] < 10 ** 9), default=None)
    peso_comida = 60.0 if (la <= lb + 1 or vida_a < 40) else 15.0
    if perto is not None:
        score += peso_comida / (perto + 1)
    alvo = perto if perto is not None else qualquer
    if alvo is not None:
        folga = vida_a - alvo
        if folga <= 0:
            score -= 2000.0  # não chega na comida a tempo
        elif folga < 20:
            score -= (20 - folga) * 40.0  # fome apertando: puxa para a comida
    elif vida_a < 30:
        score -= (30 - vida_a) * 30.0

    # Maior por 2 ou mais: aproxima a cabeça para pressionar.
    if la >= lb + 2:
        score -= 8.0 * tab.manhattan(corpo_a[0], corpo_b[0])
    return score


def minimax(tab, a, b, food, prof, alpha, beta, ply):
    tab.checar_tempo()
    melhor = -float("inf")
    for da in jogadas_plausiveis(tab, a[0]):
        pior = float("inf")
        for db in jogadas_plausiveis(tab, b[0]):
            na, nb, nf, va, vb = simular(tab, a, b, food, da, db)
            if not va and not vb:
                v = EMPATE + ply
            elif not va:
                v = -VITORIA + ply
            elif not vb:
                v = VITORIA - ply
            elif prof <= 1:
                v = avaliar_folha(tab, na, nb, nf)
            else:
                v = minimax(tab, na, nb, nf, prof - 1, max(alpha, melhor), min(beta, pior), ply + 1)
            if v < pior:
                pior = v
            if pior <= max(alpha, melhor):
                break
        if pior > melhor:
            melhor = pior
        if melhor >= beta:
            break
    return melhor


def busca_mano_a_mano(tab, notas_base):
    """Aprofundamento iterativo até acabar o tempo. Devolve nota por direção."""
    o = tab.opps[0]
    a = (tuple(c for c in tab.my_body if c >= 0), tab.health)
    b = (tuple(o["body"]), o["health"])
    food = frozenset(tab.food)
    candidatas = [d for d in range(4) if notas_base[d] > MORTE_CERTA]
    if len(candidatas) <= 1:
        return None

    resultado = None
    prof = 1
    while prof <= 12:
        try:
            atual = {}
            # Ordem: melhor da rodada anterior primeiro (corta mais galhos).
            ordem = sorted(candidatas, key=lambda d: -(resultado or notas_base)[d])
            for da in ordem:
                pior = float("inf")
                for db in jogadas_plausiveis(tab, b[0]):
                    na, nb, nf, va, vb = simular(tab, a, b, food, da, db)
                    if not va and not vb:
                        v = EMPATE
                    elif not va:
                        v = -VITORIA
                    elif not vb:
                        v = VITORIA
                    elif prof <= 1:
                        v = avaliar_folha(tab, na, nb, nf)
                    else:
                        v = minimax(tab, na, nb, nf, prof - 1, -float("inf"), pior, 1)
                    pior = min(pior, v)
                atual[da] = pior
            resultado = {d: atual.get(d, MORTE_CERTA) for d in range(4)}
        except SemTempo:
            break
        if max(resultado.values()) >= VITORIA - 100:
            break  # vitória garantida encontrada
        prof += 1
    if resultado is not None:
        logger.info("busca 1x1 profundidade %d", prof - 1 if prof > 1 else 1)
    return resultado


# ---------------------------------------------------------------------------
# Entrada
# ---------------------------------------------------------------------------

def get_move(state: GameState) -> MoveResponse:
    """POST /move — chamado a cada turno. Devolve "up", "down", "left" ou "right"."""
    inicio = time.perf_counter()
    timeout_ms = state.game.timeout or 500
    orcamento_ms = min(TETO_MS, timeout_ms * FRACAO_DO_TIMEOUT)
    tab = Tabuleiro(state, inicio + orcamento_ms / 1000.0)

    notas = [avaliar(tab, d) for d in range(4)]
    escolhido = max(range(4), key=lambda d: notas[d])

    if len(tab.opps) == 1 and tab.my_body[0] >= 0:
        busca = busca_mano_a_mano(tab, notas)
        if busca is not None:
            # Busca decide; heurística desempata.
            escolhido = max(range(4), key=lambda d: (busca[d], notas[d]))

    logger.info(
        "MOVE %d: %s | %s | %.0fms",
        state.turn,
        MOVES[escolhido],
        " ".join(f"{MOVES[d]}={notas[d]:.0f}" for d in range(4)),
        (time.perf_counter() - inicio) * 1000,
    )
    return MoveResponse(move=MOVES[escolhido])