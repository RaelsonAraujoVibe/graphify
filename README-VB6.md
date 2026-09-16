# Usando este fork do Graphify com um projeto VB6

Este fork indexa código-fonte VB6 localmente, sem precisar de uma instalação do
VB6, de um LLM ou de uma chave de API. Cada projeto VB6 instala este fork e
suas dependências Python no seu próprio ambiente e então indexa a si mesmo.
Não é necessário nenhum checkout do Graphify nem um ambiente configurado em
outra máquina.

## Declare a dependência no seu projeto VB6

Crie o arquivo `requirements-graphify.txt` na **raiz do seu projeto VB6**:

```text
graphifyy @ git+https://github.com/RaelsonAraujoVibe/graphify.git@v8
```

`@v8` aponta para a branch onde vivem as alterações deste fork para VB6. Rodando a instalação com `--upgrade` (veja "Atualize o
extrator do projeto" abaixo), o pip busca automaticamente o commit mais
recente dessa branch..

Se preferir travar numa versão específica em vez de sempre seguir a última
da branch — por exemplo, para reproduzir uma indexação antiga — substitua
`@v8` por um hash de commit ou tag publicado, como
`@4c6156f889af35f3ba77a4682e4db8ab6b3412a6`.

## Instale e indexe a partir do seu projeto VB6

Abra um terminal na raiz do projeto VB6, onde está o `requirements-graphify.txt`.
Instale antes o Python 3.11 e o Git. Os comandos abaixo criam um ambiente
dedicado para a indexação; o pip instala o Graphify e suas dependências
declaradas.

### Windows (PowerShell)

```powershell
py -3.11 -m venv .venv-graphify
.\.venv-graphify\Scripts\python.exe -m pip install -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update .
```

### macOS / Linux

```sh
python3.11 -m venv .venv-graphify
.venv-graphify/bin/python -m pip install -r requirements-graphify.txt
.venv-graphify/bin/graphify update .
```

### Instalação alternativa com uv

A partir da mesma pasta do projeto VB6, no Windows:

```powershell
uv venv .venv-graphify --python 3.11
uv pip install --python .venv-graphify\Scripts\python.exe -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update .
```

No macOS/Linux, use `.venv-graphify/bin/python` e `.venv-graphify/bin/graphify`
como executáveis do ambiente.

Ativar o ambiente é opcional: estes comandos usam diretamente o executável
instalado no projeto. Eles funcionam independentemente de qualquer instalação
global do Graphify. A instalação dos pacotes precisa de acesso à rede; a
indexação do VB6 em si roda localmente.

Adicione estas entradas ao `.gitignore` e ao `.graphifyignore` do projeto VB6
para excluir o ambiente e o índice gerado:

```gitignore
.venv-graphify/
graphify-out/
```

Se você versionar intencionalmente o índice gerado, remova `graphify-out/` do
`.gitignore`.

## Saídas da indexação

O comando `update` tanto cria o primeiro grafo quanto atualiza um já
existente. Ele executa a extração estrutural local e grava, dentro do
diretório `graphify-out/` do projeto:

- `graph.json`: nós, relacionamentos e localizações no código-fonte.
- `GRAPH_REPORT.md`: comunidades e os símbolos mais conectados.
- `graph.html`: visualização interativa do grafo, com um painel de filtros
  por facetas (tipo de nó, comunidade, tipo de relação, confiança da aresta e
  grau de conexões), busca por texto e contadores de "visível/total" por
  facet — útil para isolar só as partes do grafo que interessam num projeto
  grande.

Abra `graphify-out/graph.html` no navegador. Indexe um **diretório**, não
apenas o arquivo `.vbp`. O scanner processa os arquivos suportados abaixo
desse diretório; a associação via `.vbp` adiciona estrutura de projeto, mas
não limita a varredura aos arquivos listados nem carrega automaticamente
arquivos fora do diretório. Escolha um diretório pai comum se o projeto usa
módulos compartilhados em pastas irmãs.

## Consultar e atualizar

Dentro do diretório do projeto VB6:

```powershell
.\.venv-graphify\Scripts\graphify.exe query 'Cliente Salvar Validar'
.\.venv-graphify\Scripts\graphify.exe explain 'Form_Load'
.\.venv-graphify\Scripts\graphify.exe path 'Salvar' 'Validar'

# Atualizar após alterar o código-fonte VB6:
.\.venv-graphify\Scripts\graphify.exe update .

# Reextrair após alterar o extrator ou substituir um grafo antigo baseado em Apex:
.\.venv-graphify\Scripts\graphify.exe update . --force
```

Use nomes que existam no seu próprio projeto no lugar dos símbolos de exemplo.
No macOS/Linux, substitua pelo executável `.venv-graphify/bin/graphify`.
`--force` também permite substituir um grafo existente por um resultado
menor. Faça backup de um `graphify-out/` existente antes, se quiser manter o
grafo anterior.

Para um projeto grande, pule a clusterização/HTML e gere apenas o índice bruto:

```powershell
.\.venv-graphify\Scripts\graphify.exe update . --no-cluster
```

Depois, um `update .` normal pode gerar o relatório e a visualização. As
regras existentes do `.gitignore` são respeitadas. Adicione um
`.graphifyignore` na raiz alvo para excluir cópias de backup ou código
gerado, por exemplo:

```gitignore
backups/**
archive/**
generated/**
```

## Atualize o extrator do projeto

Como `requirements-graphify.txt` aponta para a branch `v8` em vez de um
commit fixo, não há nada para editar: `pip install --upgrade` já busca e
instala o commit mais recente dessa branch. A partir da raiz do projeto VB6,
execute:

```powershell
.\.venv-graphify\Scripts\python.exe -m pip install --upgrade -r requirements-graphify.txt
.\.venv-graphify\Scripts\graphify.exe update . --force
```

Force uma reconstrução após atualizar o extrator, para que um índice
existente seja regerado com o novo comportamento de extração. Se
`pip install --upgrade` não detectar uma mudança recente (por exemplo, por
causa de cache local), acrescente `--force-reinstall` ao comando para forçar
uma reinstalação completa a partir do commit atual da branch.

## Distribua um wheel em vez de instalar via Git

Um mantenedor pode empacotar este fork uma única vez, **a partir do checkout
do Graphify**:

```powershell
py -3.11 -m venv .venv-build
.\.venv-build\Scripts\python.exe -m pip install build
.\.venv-build\Scripts\python.exe -m build --wheel
```

Entregue o `.whl` resultante da pasta `dist/` para os usuários. No projeto
VB6 deles, o arquivo vai em `vendor/` e usa uma entrada relativa em
`requirements-graphify.txt`, por exemplo com a versão atual deste checkout:

```text
./vendor/graphifyy-0.9.61-py3-none-any.whl
```

Use o nome de arquivo real do wheel se a versão mudar. Os comandos de
instalação e indexação acima permanecem os mesmos. O pip instala as
dependências do wheel automaticamente; os usuários precisam do Python, mas
não do Git nem de um checkout do Graphify. Inclua o wheel na distribuição do
seu projeto se usar essa opção.

## Cobertura do VB6

| Arquivos | Conteúdo indexado |
| --- | --- |
| `.vbp` | Associação de projeto; dependências `Reference=` e `Object=` como nós de referência. |
| `.bas` | Módulo, procedimentos, funções, constantes, variáveis, tipos, enums, eventos e declarações `Declare`. |
| `.cls` | Classe e membros; `Property Get/Let/Set` como nós separados; referências `Implements`. |
| `.frm` | Formulário e seu código executável, incluindo procedimentos de tratamento de evento. |

Neste fork, `.cls` deliberadamente significa VB6. O dispatch de `.cls` do
Apex foi substituído; o extrator standalone de Apex e o dispatch de
`.trigger` continuam disponíveis.

O scanner trata nomes sem diferenciar maiúsculas/minúsculas, strings entre
aspas, comentários com apóstrofo/`Rem`, instruções separadas por dois-pontos
e continuações de linha com `_`. As localizações de origem referem-se às
linhas físicas originais (instruções continuadas usam a primeira linha). Ele
lê UTF-8, UTF-16 com BOM e Windows-1252. Converta projetos que usem outra
página de código ANSI para UTF-8 antes de indexar.

### Limitações atuais

- Blocos de designer, propriedades visuais, nós de controle e recursos
  binários `.frx` são ignorados. Um procedimento `cmdSave_Click` é indexado,
  mas nenhuma aresta controle-para-handler é criada.
- Arestas de chamada só vinculam alvos `Sub`, `Function` e `Declare` sem
  ambiguidade no mesmo arquivo. Chamadas entre arquivos, dispatch de
  objeto/membro, chamadas via acesso a propriedade, receptores `With`,
  membros default e COM/late binding não são resolvidos.
- `Implements` registra a interface nomeada como uma referência não
  resolvida; ainda não faz o link com a definição da classe de interface em
  outro arquivo.
- Ambos os ramos de compilação condicional são indexados. O scanner não
  avalia expressões `#If`; nomes de procedimento duplicados permanecem
  ambíguos.
- Membros de `.vbp` ausentes ou fora da raiz aparecem apenas como
  referências de arquivo. Outros formatos de origem VB6, como `.ctl`, `.pag`,
  `.dob` e `.dsr`, ainda não são interpretados.
- Isto é indexação estrutural, não um compilador nem um grafo de chamadas
  VB6 completo.

## Desenvolvimento do extrator (apenas para mantenedores)

Para trabalhar neste fork em si, a partir de um checkout do Graphify:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . pytest
.\.venv\Scripts\graphify.exe update tests/fixtures/vb6
.\.venv\Scripts\graphify.exe query 'Cliente Salvar Validar' --graph tests/fixtures/vb6/graphify-out/graph.json
.\.venv\Scripts\python.exe -m pytest tests/test_vb6.py tests/test_extractors_registry.py -q
```

Este fluxo de desenvolvimento é separado da instalação local do projeto que
os contribuidores VB6 usam.

Para validar especificamente as mudanças no painel de filtros do
`graph.html` (`graphify/exporters/html.py`), rode:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_export.py -q
```
