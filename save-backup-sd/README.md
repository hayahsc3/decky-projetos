# SaveBackupSD — plugin Decky Loader

Lista seus jogos instalados (Steam e atalhos não-Steam), faz backup dos saves para o **microSD** e restaura quando você precisar (por exemplo, depois de reinstalar o jogo).

## Como usar

1. Abra o menu rápido (botão `...`) → ícone do plugin **Save Backup SD**.
2. O topo mostra o microSD detectado e o espaço livre. Se o cartão estiver inserido mas **desmontado**, o plugin monta sozinho (chave “Montar microSD automaticamente”); também há o botão **Montar microSD**. A montagem automática vale para o backup automático e para as demais operações.
3. Toque num jogo → **Fazer backup agora**. Ou use **Backup de todos os jogos**.
4. Para restaurar: toque no jogo → escolha o backup → **Restaurar**.
   - Antes de sobrescrever, o plugin guarda um backup de segurança do estado atual (`pre-restore_…`).
   - O jogo precisa estar **fechado** e **instalado**.
   - Se você reinstalou o jogo e nunca o abriu, **abra uma vez** (para o Proton criar o prefixo) e restaure em seguida.
5. **Backup automático:** ligue a chave “Backup automático” no painel e, sempre que você fechar um jogo, o plugin salva os saves no microSD (e avisa com uma notificação). Se nada mudou desde o último backup, ele não cria outro, para não gastar espaço nem empurrar para fora backups antigos.
6. Backups de jogos que não estão mais instalados aparecem numa seção separada; ao reinstalar o jogo, ele volta para a lista com a opção Restaurar.

Os backups ficam em:

```
<microSD>/DeckSaveBackup/<appid> - <nome do jogo>/AAAA-MM-DD_HH-MM-SS.tar.gz
```

## O que é copiado

- Prefixo do Proton: `compatdata/<appid>/pfx/drive_c/users/steamuser` (sem Temp, caches e pastas do Windows) e `users/Public/Documents`.
- Cache da Steam Cloud: `userdata/<conta>/<appid>/remote`.
- Atalhos não-Steam: o prefixo do Proton deles.
- Pastas extras que você definir (veja abaixo).

**Limitações**

- Jogos **nativos do Linux** que gravam saves fora desses locais (`~/.config`, `~/.local/share`…) só são cobertos se você adicionar a pasta em `custom_paths`.
- Se o jogo usa Steam Cloud, a Steam pode sincronizar de novo ao abri-lo e sobrescrever/pedir para resolver conflito de save.
- Se o microSD não estiver conectado ao abrir o plugin, use **Atualizar** depois de inserir o cartão.

## Configurações avançadas (opcional)

Arquivo: `~/homebrew/settings/SaveBackupSD/settings.json` (criado ao mudar o controle “Backups por jogo”). Exemplo:

```json
{
  "max_backups": 5,
  "destination": "",
  "auto_backup": false,
  "auto_mount": true,
  "custom_paths": {
    "1234567": ["/home/deck/.config/MeuJogo"]
  }
}
```

- `destination`: caminho de uma pasta para usar no lugar do microSD detectado (ex.: um HD externo montado).
- `custom_paths`: por `appid`, pastas adicionais (devem ficar dentro de `/home/deck` para poderem ser restauradas).

## Versão e avisos de atualização

O painel mostra a versão instalada. A cada abertura do Decky (e a cada 6 horas) o plugin consulta as **Releases** do repositório e, se houver versão mais nova, mostra uma notificação e um aviso no painel (o botão **Verificar atualizações** força a checagem). Cada alteração enviada ao repositório gera uma release nova automaticamente (veja abaixo), então basta instalar o zip novo por cima.

Se o repositório for **privado**, o plugin pede um token do GitHub (fine-grained, só leitura de *Contents* e *Metadata* nesse repositório). Ele fica salvo em `settings.json` no Deck. Em repositório público não precisa de token.

## Compilar e gerar o ZIP instalável

Precisa de Node.js 18+ (no PC, ou no Deck em modo desktop).

```bash
npm install
npm run zip
```

Isso gera `SaveBackupSD.zip` na pasta do projeto, já na estrutura que o Decky espera (`SaveBackupSD/plugin.json`, `main.py`, `dist/index.js`…).

## Instalar no Steam Deck

1. Copie `SaveBackupSD.zip` para o Deck (por exemplo, para `/home/deck/Downloads`).
2. No modo jogo, abra o menu rápido → Decky → engrenagem (Configurações) → **Geral** → ative o **Modo desenvolvedor**.
3. Na aba **Desenvolvedor**, escolha instalar plugin a partir de um **ZIP** e selecione o arquivo. (Os nomes podem variar um pouco conforme a versão do Decky.)
4. Se o plugin não aparecer, reinicie o Decky/Steam.

Para atualizar, gere um novo ZIP e instale por cima.

## Estrutura

```
plugin.json        manifesto do Decky
main.py            backend (Python): detecção de jogos, microSD, backup e restauração
src/index.tsx      interface (React + @decky/ui)
scripts/make-zip.mjs   empacota o ZIP instalável
```

O plugin usa a permissão `root` do Decky (`flags` no `plugin.json`) apenas para conseguir montar o cartão; os arquivos restaurados voltam a pertencer ao seu usuário.

Logs do backend: `~/homebrew/logs/SaveBackupSD/`.

Licença: MIT.
