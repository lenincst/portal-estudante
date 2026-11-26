# Política de segurança e divulgação responsável

Este é um cliente **não oficial**. Ele não contorna nenhum controle de acesso do portal: cada pessoa usa somente a
própria conta e vê somente o que o portal já mostra a ela.

## Encontrou uma falha no portal (não neste código)?

Se você perceber qualquer comportamento do portal que exponha **dados de outras pessoas** (outros alunos, professores
ou funcionários), permita acessar algo além da sua própria conta ou contornar autenticação:

1. **Não explore** a falha, não baixe nem guarde os dados expostos e não teste "para confirmar".
2. **Não publique** detalhes em issues, fóruns, redes sociais ou neste repositório.
3. **Comunique em privado à instituição responsável pelo portal**, pelos canais oficiais de privacidade e proteção de
   dados (encarregado de dados/DPO) ou de suporte técnico da sua instituição, descrevendo o mínimo necessário para
   reproduzir.
4. Se a falha afetar este projeto (por exemplo, exigir uma mudança para que o cliente não acione o problema), abra uma
   issue **sem detalhes técnicos da falha**, apenas pedindo contato.

## Falha neste código

Problemas de segurança do próprio cliente (por exemplo, credenciais registradas em log, dados gravados fora da pasta
local esperada, chamadas para hosts que não sejam os do portal) podem ser relatados por issue. Não inclua senhas,
tokens, cookies, capturas de tráfego ou dados pessoais em issues.

## Princípios deste projeto

- Credenciais só na máquina do usuário (cofre do sistema operacional); nenhum servidor, nenhuma telemetria.
- A senha só é digitada na página oficial de login da Microsoft usada pelo portal.
- Somente leitura por padrão; escritas exigem ativação explícita e confirmação a cada ação; sem envio em massa.
- Ritmo de requisições fixo no código (mínimo de 1,5 s entre chamadas, sem paralelismo, recuo em 429/5xx).
