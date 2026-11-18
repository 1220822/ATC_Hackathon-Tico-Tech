# Guião da demo (3 minutos) — sexta, 20/11

> **"Uma casa que se configura a conversar, decide contigo e aprende as tuas regras."**

Casa real "Hack 7": **Sala** (central, sensor da janela, detetor de fumo, câmara) e **Quarto** (luz,
tomada com o aquecedor, sensor de temperatura e humidade). Só dados reais do MCP.

## Antes de começar (10 min antes)

1. **O gravador tem de estar a correr desde quarta** (é ele que dá dias de histórico às sugestões):
   ```powershell
   python -m src.storage.recorder --status   # leituras por divisão e métrica, último registo há poucos segundos
   ```
   Se não estiver a correr: `python -m src.storage.recorder` (fica a gravar; Ctrl+C para parar).
2. `.env`: `DRY_RUN=false` para a regra criar mesmo o cenário `TT_` no MCP e a aprovação atuar na
   tomada. Com `DRY_RUN=true` tudo funciona igual, mas nada é enviado à casa (a UI diz "modo de ensaio").
3. Arrancar tudo (API + agentes A2A + 3D + chat):
   ```powershell
   python -m src.api --agents
   ```
   Abrir **http://127.0.0.1:8000/twin**. À direita fica o chat com o Coordenador. O LLM aquece sozinho
   no arranque (a primeira resposta a frio demora ~15 s; depois, 1–2 s).
4. Estado de partida na casa real: **janela da Sala fechada**, **tomada do Quarto ligada**, luz como
   estiver. Fazer uma pergunta de aquecimento no chat ("a janela da sala está fechada?") para confirmar
   que o LLM já está quente.
5. Para limpar o que foi ensaiado: "O que a casa sabe de ti" → **Apagar tudo** (escrever APAGAR).
   Desativa os cenários `TT_` criados e apaga preferências, sugestões, decisões e conversa. O histórico
   dos sensores fica.

## O guião

| # | Tempo | O que fazer / dizer no chat | O que se vê |
|---|---|---|---|
| 1 | 0:00 | **"A janela da sala está fechada?"** | Resposta com o valor real e desde quando. *Só o Coordenador fala connosco; os agentes trabalham por trás.* |
| 2 | 0:20 | **"Quero o quarto a 21 °C de manhã"** → **Confirmar** | "Vou definir 21 °C no Quarto entre as 7h e as 9h. Confirmas?" Nada é gravado antes do sim. Fica em "O que a casa sabe de ti", com a frase original. |
| 3 | 0:45 | **"Se abrir a janela com o aquecedor ligado, avisa-me"** → **Confirmar** | O Coordenador reformula: cria o cenário **TT_janela_aberta_aquecedor_ligado** no MCP (luz do Quarto âmbar como aviso) e avisa no chat. Passa por `guard()`/`execute()`. |
| 4 | 1:10 | **Abrir a janela da Sala (a sério).** | Em ~7 s: o 3D mostra a janela aberta; aparece o aviso da regra; a Energia propõe desligar o aquecedor; o Coordenador explica e **pede aprovação** no chat (autonomia "perguntar" por omissão). **Porquê?** mostra o debate (proposta, preferência citada, decisão do Coordenador). Carregar em **Aprovar** → a tomada desliga-se. |
| 5 | 1:40 | **"Faz sempre assim"** → **Confirmar** | "Vou passar a fazer isto sozinha: desligar a tomada do Quarto quando… Só isto; o resto continua a pedir-te aprovação." Grava uma preferência `learned_confirmed`. (O mesmo acontece sozinho ao fim de 3 aprovações seguidas.) |
| 6 | 2:00 | **"Tens alguma sugestão?"** | Sugestão proativa a partir do histórico real (ex.: "A humidade no Quarto passou de 70 % nas últimas 2 noites…"). **Porquê?** desenha o gráfico real com as noites sombreadas. Não há desumidificador: a sugestão diz isso e propõe o que se pode fazer já (aviso). |
| 7 | 2:25 | **"O quarto fica à frente da sala"** → **Não** → **Ajustar à mão** | O 3D muda na hora. Ao "não", o Coordenador oferece ajustar à mão ou descrever de outra forma. No modo de edição: arrastar o Quarto, **Gravar** (e **Desfazer última**, se for preciso). |
| 8 | 2:45 | Botão **O que a casa sabe de ti** | Preferências com a frase original, a data e a origem (dita por ti / aprendida com a tua confirmação), a regra com o cenário no MCP, o histórico de alterações, editar/revogar e **Apagar tudo** (RGPD). |

## Se alguma coisa falhar

- **O LLM está lento ou em baixo**: o chat continua a funcionar, porque a classificação cai para regras
  em PT-PT. Nesse caso as propostas dos agentes não aparecem e o passo 4 fica só com o aviso da regra.
  Para o passo 5, usar o caminho das 3 aprovações em ensaio prévio.
- **O passo 6 não tem sugestão**: os detetores só sugerem com o mesmo padrão em ≥ 2 dias reais (nunca
  inventam). A resposta diz quantos dias de dados há. Se a humidade noturna ficou entre 65 e 70 %, dizer
  antes **"Humidade máxima no quarto 65 %"** → Confirmar. É uma preferência legítima, e o detetor passa a
  usá-la como limite. Depois, "Tens alguma sugestão?".
- **A proposta do passo 4 não aparece**: confirmar que a tomada está ligada (se estiver desligada, não há
  nada a propor) e que os agentes estão ligados (cabeçalho do 3D). O aviso da regra aparece de qualquer
  forma.
- **Ensaiar sem a casa real**: `python -m src.api --sim --agents` num terminal e
  `python scripts/simulate.py cenario janela` noutro. Com `SIMULATE=true` nada sai para o MCP.

## O que dizer sobre o desenho (se perguntarem)

- Só o Coordenador escreve no MCP e só em `execute()`. O fumo e a intrusão seguem um caminho fixo, sem
  LLM, que nenhuma preferência desativa. Armar e desarmar pedem sempre confirmação.
- Duas memórias em `var/home.db` (local): o **histórico de dados** (leituras a cada 30 s, eventos,
  decisões) e o **histórico de preferências** (versionado: alterar cria uma versão nova, nada é apagado
  sem o "apagar tudo"). Todos os agentes recebem um resumo compacto das duas.
- Nunca aprende em silêncio: tudo o que passa a regra ou autonomia é reformulado e confirmado.
- Contra a microgestão: no máximo 2 sugestões por dia, nada nas horas de silêncio, só com uma ação
  possível, 7 dias sem repetir uma sugestão rejeitada, e "não voltes a sugerir isto" para sempre.
