# Cachyos-Autoconfigurator

Assistente interativo para automatizar a configuração pós-instalação do CachyOS. Detecta o sistema, pede confirmação antes de qualquer alteração e faz backup dos arquivos importantes.

Ele atualiza o sistema, instala ferramentas e linguagens de desenvolvimento, configura Git, Flatpak, Bluetooth, TRIM e horário, e faz limpeza e verificações básicas de segurança. Você escolhe o que quer, em cada etapa responde Sim, Não ou Pular, e tudo fica registrado em log.

-------

## Como executar o script

Para fazer a autoconfiguração do CachyOS, rode:

```bash
curl -fsSL https://raw.githubusercontent.com/dyalncerqueira827-dot/Cachyos-Autoconfigurator/main/install.sh | sh
```

Quer só simular, sem alterar nada? Adicione `-s -- --dry-run`:

```bash
curl -fsSL https://raw.githubusercontent.com/dyalncerqueira827-dot/Cachyos-Autoconfigurator/main/install.sh | sh -s -- --dry-run
```

> ⚠️ O programa altera o sistema e precisa de `sudo`. Teste primeiro com `--dry-run` e leia o código antes de usar.
