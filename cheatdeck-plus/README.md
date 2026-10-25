# CheatDeck Plus

Plugin do Decky Loader para executar trainers **FLiNG** (e outros `.exe`) no mesmo prefixo Proton do jogo que está rodando, direto do Modo Jogo do Steam Deck.

## Recursos
- Detecta o jogo em execução e seu prefixo Proton (`STEAM_COMPAT_DATA_PATH`).
- Lança o trainer dentro do container do jogo (`steam-runtime-launch-client`) ou direto via `proton run`; modo automático com fallback.
- Busca trainers no flingtrainer.com e baixa/importa (`.exe`, `.zip`, `.rar`, `.7z`).
- Importa arquivos de `~/Downloads` e `~/Desktop`, ou baixa por link.
- Fecha o trainer automaticamente quando o jogo termina.
- Diagnóstico com log na própria interface.

Trainers ficam em `~/.local/share/cheatdeck-plus/trainers/<appid>/`.

## Build
```
pnpm install
pnpm build          # gera dist/index.js
pnpm package        # gera out/CheatDeck-Plus.zip
```
Instale o zip em Decky > Configurações > Desenvolvedor > Instalar plugin de ZIP.
Para `.rar`/`.7z` é preciso ter `7z`, `bsdtar` ou `unrar` no Deck.

## Uso
1. Abra o jogo. 2. Abra o menu de acesso rápido (...) > CheatDeck Plus. 3. Baixe/importe o trainer e toque em ▶. 4. Troque para a janela do trainer pelo menu Steam > Trocar janela.
