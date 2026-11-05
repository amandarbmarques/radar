# Detector de furos — coletor de manchetes

Roda sozinho no GitHub Actions, de 2 em 2 horas (6h às 22h, horário de Brasília). Abre as páginas de política e economia e as colunas de bastidor de Folha, O Globo, Valor e Estadão. Grava as manchetes novas na aba **Manchetes** de uma planilha do Google, com título, linha fina, horário de publicação, pontuação e os sinais que levaram à pontuação.

A coluna **Candidato** marca o que passou do filtro de regras. A triagem final (é furo ou não, e quem deu primeiro) é feita depois, na revisão.

## Arquivos

- `coletar_manchetes.py`: o coletor.
- `config.yaml`: veículos, páginas, padrões de link e regras de pontuação. Ajustes do dia a dia são feitos só aqui.
- `requirements.txt`: bibliotecas.
- `.github/workflows/coletar.yml`: agendamento no GitHub Actions.

## Instalação (uma vez)

1. **Repositório.** Crie um repositório no GitHub (pode ser privado) e suba os arquivos mantendo a pasta `.github/workflows/`.

2. **Planilha.** Crie uma planilha no Google Sheets e copie o ID dela. O ID é o trecho da URL entre `/d/` e `/edit`. A aba `Manchetes` é criada automaticamente.

3. **Conta de serviço do Google.** É o "robô" que escreve na planilha.
   - No Google Cloud Console, crie um projeto e ative a **Google Sheets API**.
   - Em *IAM e administrador → Contas de serviço*, crie uma conta e gere uma chave **JSON**.
   - Compartilhe a planilha com o e-mail da conta de serviço (termina em `iam.gserviceaccount.com`) como **Editor**.
   - Se o Google Workspace da empresa bloquear a criação de projetos ou chaves, peça à Tecnologia.

4. **Secrets do GitHub.** No repositório, vá em *Settings → Secrets and variables → Actions → New repository secret* e crie:
   - `GOOGLE_SERVICE_ACCOUNT_JSON`: o conteúdo inteiro do arquivo JSON.
   - `PLANILHA_ID`: o ID da planilha.

   O arquivo JSON nunca deve ser enviado para o repositório (o `.gitignore` já bloqueia).

5. **Primeiro teste.** Em *Actions → Coletar manchetes → Run workflow*, marque **Só testar** e rode. No log aparece quantos links cada página rendeu e as candidatas a furo. Se uma página vier com 0 links, o site mudou o endereço, bloqueou o acesso ou o padrão de link precisa de ajuste. O CSV da execução fica disponível para download no fim da página da execução.

6. Depois do teste, rode sem marcar **Só testar**. A partir daí, o agendamento cuida do resto.

## Ajustes comuns

- **Incluir um veículo ou coluna:** copie um bloco em `fontes` no `config.yaml` e rode o teste.
- **Muito ruído ou furo escapando:** mude `minimo_candidato` ou os termos e pesos em `pontuacao`.
- **Horários:** edite as linhas `cron` do workflow. Os horários são em UTC (Brasília + 3h).

## Limites conhecidos

- O horário de publicação vem das meta tags das páginas. Quando o site não informa, vale o "Coletado em", com precisão de cerca de 2 horas.
- Em matérias com paywall, o coletor só vê título e linha fina.
- Sites podem bloquear acessos vindos dos servidores do GitHub. O teste mostra isso.
- O GitHub pode atrasar execuções agendadas em horários de pico. Em repositórios públicos, o agendamento é desligado após 60 dias sem atividade no repositório.
