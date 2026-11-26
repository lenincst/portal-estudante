# portal-estudante

**Cliente não oficial, em Python, compatível com o portal acadêmico Ulife.** Permite consultar pelo código o que você já
vê no portal com a sua própria conta.

> ## ⚠️ Aviso de isenção de responsabilidade — leia antes de usar
>
> - **Projeto NÃO OFICIAL.** Não tem qualquer vínculo, afiliação, patrocínio ou aprovação da Ânima Educação, da Unisul,
>   do Ulife ou de qualquer instituição de ensino. Os nomes e marcas citados pertencem aos seus titulares e aparecem
>   aqui só para descrever com qual portal o cliente é compatível.
> - O software é fornecido **"no estado em que se encontra", sem garantia de nenhum tipo** (veja a licença Apache-2.0).
>   Os autores e colaboradores **não se responsabilizam** por qualquer dano, perda de dados, bloqueio ou suspensão de
>   conta, sanção acadêmica ou disciplinar, ou qualquer outra consequência do uso.
> - **Cada usuário é o único responsável** pelo próprio uso: por conferir e cumprir os Termos de Uso do portal, o
>   regimento e o contrato da sua instituição e a legislação aplicável, e por proteger as próprias credenciais.
> - O uso é permitido **somente com a sua própria conta**. É **proibido** usar este projeto para acessar contas de
>   terceiros, para fins acadêmicos desonestos, para envio em massa de mensagens ou para sobrecarregar o portal.
> - **O projeto sai do ar se a instituição pedir.**

## Cuidados de segurança (em linguagem simples)

- Suas credenciais ficam **só no seu computador**. Por padrão a senha **não é guardada**: você a digita na página
  oficial de login, como faria no navegador.
- **Não compartilhe sua senha** com ninguém, nem com este projeto em outro computador.
- **Não rode versões modificadas por terceiros** sem revisar o código: uma cópia alterada poderia enviar sua senha para
  outro lugar. Este código só fala com o próprio portal e com a página oficial de login da Microsoft.
- Nenhum servidor, nenhuma telemetria: nada é enviado a lugar algum além do portal.

## O que dá para fazer

Ver cursos, disciplinas, notas, aulas e atividades, prazos, calendário de aulas, aulas gravadas, materiais (com
download), avisos, notificações (inclusive em tempo real), mensagens, documentos de matrícula, histórico, progresso,
certificados, preferências de notificação e serviços de atendimento. Algumas ações que **gravam** algo (enviar
mensagem, abrir solicitação com anexo, enviar documento, trocar a foto, mudar preferências) existem, mas ficam
**desativadas por padrão** e exigem ativação explícita **e** confirmação a cada ação; há também um **modo de
simulação** que mostra o que seria enviado sem enviar nada.

**Não existe** (de propósito): matrícula em disciplinas, registro de presença, envio de atividades ou avaliações, envio
em massa ou qualquer função que dê vantagem acadêmica.

## Requisitos

- Windows, macOS ou Linux com **Python 3.11, 3.12 ou 3.13**.
- Uma conta ativa no portal (login no formato `RA@ulife.com.br`).
- Cerca de 400 MB livres (navegador Chromium usado só para a tela de login).

## Instalação passo a passo

```bash
git clone <endereço deste repositório>.git     # o mesmo endereço da página do GitHub onde você está lendo isto
cd portal-estudante
python -m venv .venv
# Windows:            .venv\Scripts\activate
# macOS / Linux:      source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m playwright install chromium
```

Tudo o que o programa grava (sessão cifrada, perfil do navegador de login, controle de notificações) fica fora da pasta
do projeto: `%LOCALAPPDATA%\portal-estudante` no Windows ou `~/AppData/Local/portal-estudante` nos demais sistemas.

## Entrar com a sua conta

```bash
python -m portal_estudante setup     # pergunta só o seu login (RA); a senha NÃO é guardada
python -m portal_estudante login     # abre a página oficial: digite sua senha lá
python -m portal_estudante status    # mostra o que está configurado (nunca os valores)
python -m portal_estudante check     # testa todas as leituras (somente leitura)
```

- O `login` abre o navegador na página oficial da Microsoft usada pelo portal; você digita a senha lá. O programa só
  guarda a **sessão** resultante, cifrada (a chave fica no cofre do sistema: Gerenciador de Credenciais do Windows,
  Keychain do macOS ou Secret Service no Linux). Quando a sessão expira, o navegador abre de novo.
- **Opcional:** `setup --save-password` guarda a senha **no cofre do sistema operacional**, para logins sem interação.
  Em scripts, `--password-stdin` lê a senha da entrada padrão em vez de perguntar. Senhas e tokens nunca aparecem em log.
- A senha só é digitada pelo programa (no modo opcional) na página `login.microsoftonline.com` enquanto ela atende ao
  login do portal: o código confere o identificador do aplicativo e o endereço de retorno antes de digitar.
- Se o seu login exigir segundo fator (MFA), aprove-o no navegador que abrir.

## Uso rápido

```python
from portal_estudante.client import PortalClient

with PortalClient() as c:                      # "with" guarda a sessão renovada ao sair
    for d in c.disciplines():
        print(d["DisciplineName"], d["GradeName"])
    d = c.disciplines()[0]
    notas = c.grades(d)
    nao_lidas = c.notifications(unread_only=True)
```

**Ritmo fixo:** o cliente espera **pelo menos 1,5 s (mais uma variação aleatória) entre requisições**, faz uma de cada
vez e, se o portal responder 429 ou 5xx, espera 2, 4, 8 e 16 s antes de desistir. Isso é fixo no código. Uma
verificação completa leva poucos minutos.

## Catálogo de ações

Convenções: `c = PortalClient()`; `s = c.services`; `m = c.messages`; `d` = um item de `c.disciplines()`.
**L** = leitura. **E** = escrita: desativada por padrão; exige `PortalClient(allow_writes=True)` **e** `confirm=True`
na chamada. Sem isso levanta `WriteBlockedError` mostrando o que seria enviado. Com `PortalClient(dry_run=True)` toda
escrita devolve `{"dry_run": True, "action": ..., "would_send": {...}}` e **nada é enviado**.

Erros possíveis em qualquer ação: `SessionExpiredError` (o cliente tenta entrar de novo sozinho), `MfaRequiredError`,
`RateLimitedError`, `EndpointGoneError` / `SchemaChangedError` / `PageStructureChangedError` (o portal mudou — veja
"Mudanças no portal"), `SecretsError` (falta rodar `setup`).

### Cursos, disciplinas e conteúdo
| Ação | Função | Parâmetros / pré-requisitos | Retorno | Tipo |
|---|---|---|---|---|
| Meus cursos | `c.courses()` | — | `CourseName`, `OrganizationName`, `CourseStatus` | L |
| Disciplinas | `c.disciplines(include_inactive=False)` | — | lista; `IsMainClass` marca a turma principal de cada disciplina | L |
| Prazos (página inicial) | `c.activities()` | — | `LessonCategoryName`, `DisciplineName`, `DeliveryDate` | L |
| Atividades avaliativas | `s.evaluative_activities(tab=0)` | — | todas as páginas | L |
| Professores | `c.teachers()` | — | `Name`, `ClassDisciplineID` | L |
| Notas | `c.grades(d)` | `d` | atividades com notas | L |
| Aulas e atividades | `c.lessons(d)` | `d` | aulas com suas atividades (não abre o player, que registraria progresso) | L |
| Materiais | `c.materials(d)` | `d` | `Title`, `Url`, `Extension`, `CanDownload` | L |
| Baixar material | `c.download(url, destino)` | `Url` de um material | caminho salvo | L |
| Lista da turma | `c.members(d)` | **desativada por padrão**: `PortalClient(classmates=True)` | só `name`, `role`, `user_organization_id`; nada é salvo | L |
| Categorias para falar com o professor | `c.communication_categories(d)` | `d` | `CommunicationCategoryId`, `CommunicationSubcategoryId` | L |
| Mensagem aos professores da disciplina | `c.send_teacher_message(d, texto, category_id, subcategory_id, confirm=True)` | categoria acima; texto com ≥ 4 palavras e ≥ 10 letras | resposta do portal | E |

```python
d = next(x for x in c.disciplines() if x["IsMainClass"])
for mat in c.materials(d):
    if mat["CanDownload"] and not mat["IsLink"]:
        c.download(mat["Url"], mat["Title"])
```

### Calendário e aulas
| Ação | Função | Parâmetros | Retorno | Tipo |
|---|---|---|---|---|
| Aulas da semana | `c.class_schedule(start=None, end=None)` | `datetime.date` (padrão: semana atual) | aulas com datas e horários | L |
| Aulas gravadas | `c.recorded_classes(months_back=6)` | — | data, disciplina, turma, horários | L |
| Período letivo | `c.current_term()` | — | `dataInicio`, `dataFim` | L |
| Compromissos de hoje | `c.commitments()` | — | aulas e eventos do dia | L |

### Mensagens
| Ação | Função | Parâmetros | Retorno | Tipo |
|---|---|---|---|---|
| Caixa de entrada | `m.inbox()` | — | remetente, assunto, `is_read`, tamanho da conversa | L |
| Enviadas | `m.outbox()` | — | idem | L |
| Ler conversa | `m.thread(resumo, allow_mark_read=False)` | item de `inbox()` / `outbox()` | mensagens com remetente, data, destinatários e corpo (`body_text`) | L (*) |
| Escrever mensagem (uma pessoa) | `c.send_direct_message(user_organization_id, assunto, corpo_html, confirm=True)` | **um** destinatário por chamada (id de um professor ou, com opt-in, de `members()`); assunto e corpo não vazios | `{"Success": true}`; aparece em `m.outbox()` | E |

(*) Abrir uma conversa **recebida e ainda não lida** a marca como lida no portal; por isso exige `allow_mark_read=True`.
Responder dentro da mesma conversa e marcar como lida/apagar ainda não existem — responda com uma nova mensagem.

### Avisos e notificações
| Ação | Função | Parâmetros | Retorno | Tipo |
|---|---|---|---|---|
| Avisos | `c.announcements(max_pages=1)` | `max_pages=None` = todas | avisos; **não** marca como lido | L |
| Resumo | `c.notifications_summary()` | — | `UnReadCount` e as mais recentes | L |
| Notificações | `c.notifications(unread_only=False, search="", categories=None, max_pages=1)` | ids de `notification_categories()` | lista; listar não marca como lida | L |
| Categorias | `c.notification_categories()` | — | `NotificationCategoryId`, `Name` | L |
| Marcar como lidas | `c.mark_notifications_read([id, ...], confirm=True)` | ids das notificações | resposta | E |
| Preferências | `s.notification_preferences()` | — | por evento: `EventId`, `EmailIsOn`, `PageIsOn`, `MobileIsOn` | L |
| Mudar uma preferência | `s.set_notification_preference(event_id, canal, ligado, confirm=True)` | canal: `email`, `sms`, `mailbox`, `page` ou `mobile` | resposta | E |
| **Receber notificações ao chegar** | `python -m portal_estudante notifications` ou `NotificationWatcher(c, callback).run()` | — | chama o callback a cada notificação nova | L |

```python
from portal_estudante.client import PortalClient
from portal_estudante.notifications import NotificationWatcher

NotificationWatcher(PortalClient(), lambda n: print("Nova:", n["Title"])).run()
```
Funcionamento: escuta o canal em tempo real que o próprio portal usa (SignalR), sem anunciar você como "online" no
chat; a cada mudança busca a lista e entrega só as novas. Como segurança, consulta de novo a cada 10 minutos (o
intervalo mínimo é fixo em 5 minutos). Na primeira execução, as notificações já existentes são marcadas como vistas
(não são entregues).

### Documentos, histórico e serviços acadêmicos
| Ação | Função | Parâmetros / pré-requisitos | Retorno | Tipo |
|---|---|---|---|---|
| Documentos de matrícula | `c.enrollment_documents()` | — | `nomeTipoDocumento`, `statusDocumento`, `alunoDocumentoId`, `codigoTipoDocumentoSiaf` | L |
| Baixar documento de matrícula | `s.download_enrollment_document(alunoDocumentoId, destino)` | id acima | caminho salvo | L |
| Enviar documento de matrícula | `s.upload_enrollment_document(codigoTipoDocumentoSiaf, arquivo, confirm=True)` | **JPG, PNG ou PDF de até 10 MB** | resposta | E |
| Histórico escolar | `s.academic_record()` | — | dados da matrícula | L |
| Declaração de regularidade (matrículas elegíveis) | `s.enrollment_declaration_eligibility()` | — | lista | L |
| Percentual de hibridez | `s.hybridity()` | — | cálculo por disciplina | L |
| Progresso geral | `s.progress()` | — | cursos, períodos, indicadores | L |
| Cursos e períodos (secretaria) | `s.siaf_courses()` | — | lista | L |
| Microcertificados | `s.certificates()` | — | lista | L |
| Política de bolsas (PDF) | `s.scholarship_policy_pdf(destino)` | — | caminho do PDF | L |
| Bibliotecas online | `s.online_libraries()` | — | bibliotecas disponíveis | L |
| Seus dados básicos | `s.identity()` | — | RA, nome e e-mail (vêm do seu próprio login) | L |

### Solicitações (atendimento) — por exemplo, enviar um atestado
| Ação | Função | Parâmetros / pré-requisitos | Retorno | Tipo |
|---|---|---|---|---|
| Tipos | `s.request_types(coordination=False)` | — | `id`, `nome`, `salesforceId` | L |
| Motivos de um tipo | `s.request_reasons(type_id)` | `id` de `request_types()` | lista | L |
| Assuntos de um motivo | `s.request_subjects(reason_id)` | `id` de `request_reasons()` | lista | L |
| Abrir solicitação | `s.create_request(tipo, motivo, assunto, mensagem, files=[...], phone="", confirm=True)` | itens das três listas; anexos **PDF, PNG ou JPG, até 24 arquivos, soma < 10 MB** | resposta | E |
| Chamado de suporte técnico | `c.create_support_ticket(titulo, descricao_html, categoria, confirm=True)` | categoria de `c.support_categories()` | resposta (nunca envia seus cookies) | E |
| Atualizar telefone ou e-mail | `s.update_contact(phone="", email="", confirm=True)` | ao menos um | status | E |

**Atestado:** o portal não tem um tipo "atestado"; o caminho para justificar faltas com documento é
*Acadêmico → Disciplinas/UC, Aulas, Notas e Faltas → Notas e Faltas*:
```python
c = PortalClient(dry_run=True)            # troque por allow_writes=True para enviar de verdade
s = c.services
tipo = next(t for t in s.request_types() if t["nome"] == "Acadêmico")
motivo = next(r for r in s.request_reasons(tipo["id"]) if r["nome"].startswith("Disciplinas/UC"))
assunto = next(a for a in s.request_subjects(motivo["id"]) if a["nome"] == "Notas e Faltas")
print(s.create_request(tipo, motivo, assunto, "Envio atestado médico referente a ...", files=["atestado.pdf"], confirm=True))
```

### Perfil
| Ação | Função | Pré-requisitos | Tipo |
|---|---|---|---|
| Trocar a foto | `s.upload_profile_photo(imagem, crop=None, confirm=True)` | **.jpg, .jpeg, .gif ou .png; até 4 MB; maior que 300×300 px**. Aceita em seu nome a declaração do portal ("tenho os direitos sobre a imagem e não violo os Termos de Uso"). Recorte padrão: o maior quadrado central (a imagem inteira, se for quadrada). Usa o navegador. | E |
| Nome, e-mail, nascimento, organização padrão | — | não são editáveis pelo aluno no portal (telefone/e-mail: `s.update_contact`) | — |

### O que não está disponível (e por quê)
Abrir o player de aulas e atividades (registraria progresso), chat (anunciaria presença online), fórum e páginas
antigas (links quebrados no próprio portal), desafio ENADE e detalhe do curso (a página grava algo ao abrir), emissão do
PDF da declaração, plano de ensino, histórico e comentários de solicitações, pagamentos e benefícios (sistemas
externos), reprodução de aulas gravadas, sair (logout), matrícula e qualquer envio de atividade ou avaliação.

## Mudanças no portal (detecção automática)

O portal pode mudar sem aviso. Cada resposta é comparada com o formato esperado:
- **Quebrou** (campo usado sumiu ou mudou de tipo, endereço 404/410, tela sem os marcadores esperados): erro específico
  — `SchemaChangedError`, `EndpointGoneError` ou `PageStructureChangedError` — com o caminho do campo e a função que
  precisa ser atualizada.
- **Mudou sem quebrar** (campo novo, ou campo não usado que sumiu): é aceito automaticamente e registrado em
  `contracts/drift.jsonl`, na pasta de dados local.
- Para verificar tudo de uma vez, só com leituras: `python -m portal_estudante check`.

## Limitações
- Usa endereços internos do portal que não são documentados e podem mudar a qualquer momento.
- Testado com uma conta de graduação; outras modalidades podem ter telas diferentes.
- Algumas escritas foram mapeadas a partir da interface sem serem executadas de verdade (envio de documento, solicitação,
  preferências, contato, mensagem ao professor); trate-as com cuidado e use `dry_run=True` antes.
- Se o login exigir segundo fator toda vez, será preciso aprová-lo no navegador a cada novo login.

## Desenvolvimento
```bash
python -m pip install -e ".[dev]"
python -m pytest            # testes automáticos (dados sintéticos; não acessam o portal)
```
Estrutura: `src/portal_estudante/` (biblioteca e linha de comando) e `tests/`.

## Licença
[Apache-2.0](LICENSE). A licença não concede direito de uso de marcas.
