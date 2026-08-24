# -*- coding: utf-8 -*-
"""
============================================================
REGRAS DE CATEGORIZAÇÃO — hpc_event_report.py
============================================================
Este arquivo define, num só lugar, as categorias usadas para classificar
os eventos do Monit (coluna "Detalhes" do CSV) e as expressões regulares
(regex) que identificam cada uma. O script principal (hpc_event_report.py)
só importa a lista RULES daqui — não é preciso editar o script para
ajustar regex ou adicionar categoria nova.

A ORDEM da lista RULES importa: ela define, ao mesmo tempo,
  1) a prioridade usada para ordenar o ranking de hosts/clusters
     (categoria mais à frente na lista = mais prioridade no TOP);
  2) a ordem padrão das colunas nos relatórios (CSV, texto, markdown
     e HTML). No HTML, o usuário pode reordenar/ocultar colunas pela
     própria página ("Configurar colunas"), mas essa lista aqui é o
     ponto de partida.


COMO ADICIONAR UMA NOVA REGRA A UMA CATEGORIA JÁ EXISTENTE
------------------------------------------------------------
Edite o "pattern" da categoria correspondente, adicionando a nova
alternativa com "|". Exemplo — mais um código de erro de GPU:

    {
        "category": "gpu",
        "label": "GPU",
        "pattern": r"GPU\\s*-\\s*NVIDIA|NVRM|NVLINK|\\[TST23-02\\]|\\[TST23-04\\]|"
                   r"\\[TST23-05\\]|\\[TST23-08\\]|\\[TST23-10\\]|\\[TST23-99\\]",
                                                                    # ^ novo código adicionado aqui
    },


COMO CRIAR UMA CATEGORIA NOVA
--------------------------------
1. Escolha uma chave curta, em minúsculas, sem espaços e sem acentos
   (ex.: "disco", "rede", "memoria"). Essa chave é usada internamente
   (nomes de coluna no CSV, contadores, etc.) — não aparece pro usuário.

2. Escolha um "label" — o nome bonito que aparece nos relatórios e na
   interface (ex.: "Disco", "Rede", "Memória").

3. Escreva o "pattern" (regex) que identifica essa categoria no texto
   de "Detalhes".

4. (Opcional) "exclude_if_matched": lista de categorias que, se já
   tiverem batido no MESMO evento, fazem essa regra não ser aplicada.
   Hoje é usado por "sensor_temp", que não deve contar de novo um
   evento que já foi classificado como "gpu_temp" (evita contar o
   mesmo alerta de temperatura duas vezes). Só funciona se a categoria
   listada em "exclude_if_matched" aparecer ANTES na lista RULES.

5. Adicione o dicionário à lista RULES, na posição que corresponde à
   prioridade desejada (mais crítico = mais no topo da lista).

EXEMPLO — adicionando a categoria "disco":
------------------------------------------
    RULES.append({
        "category": "disco",
        "label": "Disco",
        "pattern": r"DISK\\s*SPACE|FILESYSTEM\\s*FULL|\\[TST3\\d-\\d\\d\\]",
    })

Assim que você adicionar, a nova categoria passa a aparecer
automaticamente: nas colunas do CSV/texto/markdown/HTML, no seletor
"Configurar colunas" do report.html e na priorização do ranking —
não precisa mexer em mais nada no hpc_event_report.py.


DICAS DE REGEX
-----------------
- Todas as regras já rodam com re.IGNORECASE (maiúscula/minúscula
  não importa) — não precisa se preocupar com isso no pattern.
- "\\b" delimita palavra inteira: r"\\bIB\\b" bate em "IB" isolado,
  mas não em "IBM" ou "CALIBRE".
- "\\[TST23-07\\]" busca literalmente o código entre colchetes (os
  colchetes precisam do "\\" na frente porque são caracteres
  especiais de regex).
- "\\s*" entre palavras tolera zero ou mais espaços variáveis
  (ex.: "GPU - TEMPERATURA" ou "GPU-TEMPERATURA" ambos batem).
- Teste uma regra isoladamente antes de usar em produção, por
  exemplo, no terminal:

    python3 -c "import re; print(re.search(r'SEU_PADRAO_AQUI', 'texto de teste do Monit', re.IGNORECASE))"

  Se imprimir "None", a regra não bateu; se imprimir um objeto
  Match, bateu certinho.
============================================================
"""

RULES = [
    {
        "category": "gpu",
        "label": "GPU",
        "pattern": (
            r"GPU\s*-\s*NVIDIA|NVRM|NVLINK|"
            r"\[TST23-02\]|\[TST23-04\]|\[TST23-05\]|\[TST23-08\]|\[TST23-10\]"
        ),
    },
    {
        "category": "gpu_temp",
        "label": "GPU Temp",
        "pattern": r"GPU\s*-\s*TEMPERATURA|\[TST23-07\]",
    },
    {
        "category": "sensor_temp",
        "label": "Sensor Temp",
        "pattern": r"TEMPERATURA|\[TST13-01\]",
        # Não conta de novo um evento que já foi classificado como "gpu_temp"
        "exclude_if_matched": ["gpu_temp"],
    },
    {
        "category": "ib",
        "label": "IB",
        "pattern": r"\bIB\b|INFINIBAND|\bib0\b|\bib1\b|IB\s*-",
    },
]
