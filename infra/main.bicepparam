using './main.bicep'

param location = 'northcentralus'
param sreLocation = 'eastus2'
param resourcePrefix = 'contoso-agents'
param projectNames = [
  'travel'
  'support'
  'research'
  'platform'
]
param githubRepository = 'ericchansen/microsoft-foundry'
param githubEnvironment = 'contoso-agents'
param budgetContactEmails = [
  readEnvironmentVariable('BUDGET_CONTACT_EMAIL')
]
param monthlyBudgetUsd = 500
param tags = {
  project: 'contoso-agents'
  'managed-by': 'microsoft-foundry-demo'
  boundary: 'rg-contoso-agents'
  'cost-centre': 'demo'
}