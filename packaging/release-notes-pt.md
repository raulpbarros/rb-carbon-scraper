Programa para gerar a planilha de projetos de carbono. Não precisa instalar
nada e não precisa de Python: baixe, descompacte e abra.

Esta versão traz **9.619 projetos** de quatro registros — Verra VCS (5.245),
Gold Standard (4.141), Cercarbono (231) e Plan Vivo (2) — já dentro do
programa. A planilha sai em segundos, sem internet.

## Antes de extrair: desbloqueie o .zip

Clique com o botão direito no arquivo `.zip` baixado → **Propriedades** → marque
**Desbloquear** → OK. **Só depois** extraia.

O Windows marca tudo o que vem da internet, e enquanto essa marca existir o
programa abre com a tela azul *"O Windows protegeu o seu computador"*, com o
botão de executar escondido atrás de **Mais informações**. Desbloquear o `.zip`
tira a marca de uma vez só, antes de qualquer coisa ser executada.

Se você já extraiu sem desbloquear: apague a pasta extraída, volte ao `.zip`,
faça o passo acima e extraia de novo.

## O que baixar

| Arquivo | Para quem |
|---|---|
| `CarbonRegistryScraper-0.2.0-portable.zip` | **todo mundo.** Descompacte e dê dois cliques no `CarbonRegistryScraper.exe` |
| `CarbonRegistryScraper-0.2.0-setup.exe` | quem prefere instalar e ter atalho no menu Iniciar |
| `carbon-seed.db` | só para quem vai rodar pelo código-fonte (veja o README) |

Extraia em **Documentos**, não em *Arquivos de Programas*.

Dentro do `.zip` há um **LEIA-ME.txt** com o passo a passo completo.

## Como usar

- **[Export Excel]** — gera a planilha com os dados que já vêm no programa.
  Segundos, sem internet. É o botão do dia a dia.
- **[Update registry data...]** — busca dados novos nos sites dos registros.
  Leva horas, mostra a estimativa antes de começar e pode ser cancelado a
  qualquer momento sem perder nada. Não gera planilha: depois de atualizar,
  clique em [Export Excel].

A planilha vai para `Documentos\Carbon Registry` e **cada exportação cria um
arquivo novo** (`carbon-projects_v1.xlsx`, `_v2`, ...). Nenhuma planilha já
enviada é sobrescrita.

Seus dados ficam em `%LOCALAPPDATA%\CarbonRegistryScraper` e sobrevivem a uma
atualização do programa: para atualizar, apague a pasta extraída e descompacte
a versão nova.

## Se algo der errado

O registro do que aconteceu fica em
`%LOCALAPPDATA%\CarbonRegistryScraper\logs\gui.log`. Janelas de erro mostram o
caminho de um arquivo específico daquela falha — envie ele junto com o que
você estava fazendo.
