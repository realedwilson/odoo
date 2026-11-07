# SDR FaleMulti para Odoo 20

Módulo de homologação. Por padrão o cron de saída está inativo e o transporte real não existe.
O código não implementa login, webhook ou endpoints privados do FaleMulti. Simular entrega não comprova integração.

## Instalação de teste

Em um ambiente Linux com as dependências do checkout Odoo e PostgreSQL, use banco descartável:

    ./odoo-bin -d sdr_test -i crm_sdr_falemulti --without-demo=True --test-enable --test-tags /crm_sdr_falemulti --stop-after-init

Teste isolado, sem dependências de Odoo:

    python addons/crm_sdr_falemulti/tests/test_standalone.py

## Operação simulada

1. Atribua o grupo SDR Supervisor e cadastre uma fila com operadores.
2. Crie uma campanha, uma oportunidade da mesma empresa e uma conversa vinculada ao ticket.
3. Marque elegibilidade somente após revisão; Retomar ativa a conversa, independentemente da pausa da campanha.
4. Crie uma intenção em Revisão e envios. Aprovar congela o texto e registra versão da conversa e política.
5. Eventos verificados de resposta, tomada humana e opt-out invalidam pendências. O opt-out é terminal.
6. Na aba Políticas, registre avaliação e publique ou restaure uma versão anterior. A versão operacional sempre sobe, invalidando revisões antigas.

## Integração futura e limites

O adaptador recebe a identidade (conta, canal, fila, ticket, contato).
snapshot deve trazer a identidade, versão reconciliada da conversa, human e opt_out booleanos,
e window_until UTC ou templates homologados. O adaptador não deve fabricar versão local quando
não puder confirmar que leu todos os eventos externos. send recebe chave durável e devolve
confirmed com ID externo, rejected somente com certeza de não envio, ou uncertain.
A entrada privada _ingest_verified só pode ser chamada após validar autenticação, identidade e
proveniência. Nunca expor essa função como webhook sem autenticação e autorização.

SDR_OPENAI_API_KEY e SDR_OPENAI_MODEL habilitam somente a extração de fatos quando o fluxo
de qualificação é invocado. Usar modelo compatível com resposta JSON. Não versionar chaves.
O extrator envia contexto para a API externa; no piloto use dados sintéticos até validar retenção
e escopo. O modelo não tem ferramentas ou acesso à fila. O conteúdo das respostas não é salvo
como transcrição pelo conector; os fatos preservam o ID do evento como evidência.

A homologação real exige prova de leitura de respostas e mensagens manuais, envio e confirmação
no mesmo ticket e isolamento por identidade e fila. Até lá o módulo fica em rascunho/revisão.
Os limites de horário e volume são conservadores. O orçamento de API, criação automática de
cadência, avaliações comparativas automatizadas e liberação autônoma exigem as próximas entregas.


## Qualificação nativa, sem WhatsApp

Abra CRM → oportunidade → Qualificação IA e registre o que o cliente informou, ou crie a interação
em SDR FaleMulti → Qualificação no Odoo. A oportunidade deve ter empresa e vendedor responsável.
O agendador ativo analisa uma interação por ciclo, preserva evidências textuais e sugere a próxima
pergunta. Ação Corrigir fato tem precedência nas próximas interações da mesma oportunidade.
Falhas e casos sem evidência ficam para revisão. Use Melhorias do agente para publicar orientações
depois de registrar as evidências e a avaliação humana; versões anteriores podem ser restauradas.

SDR_LOCAL_MODEL configura o modelo no Ollama interno, na rede de containers. O modelo não recebe
credenciais nem faz envios. Qualificação nativa e transporte WhatsApp são independentes.
