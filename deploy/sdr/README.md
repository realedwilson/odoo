# VPS SDR

O sistema monitora exclusivamente a branch codex/crm-sdr-falemulti deste fork.
O timer busca o GitHub a cada 5 minutos e aceita apenas avanço linear do commit ativo.
Cada candidato é instalado e testado em banco separado antes da atualização.
Antes da migração, o serviço é parado e banco e filestore são copiados. Se a migração ou
a verificação HTTP falhar, a versão anterior e os dados pré-migração são restaurados.

No servidor: systemctl status odoo-sdr-update.timer; journalctl -u odoo-sdr-update.service.
Para suspender atualizações sem parar Odoo: systemctl stop odoo-sdr-update.timer.
O host usa /opt/odoo-sdr; o arquivo deployed.commit só muda após verificação da versão nova.
Mudanças em Dockerfile e nas unidades operacionais precisam de revisão no host.

A aplicação escuta apenas no loopback da VPS. O acesso inicial é via túnel SSH.
PostgreSQL e Ollama ficam na rede privada Docker. Segredos ficam fora do Git.
Backups de atualização estão em /opt/odoo-sdr/backups; mantenha também cópia externa.
Não ative o cron FaleMulti antes de homologar o canal e a identidade de atendimento.
