#!/bin/bash
# Deploy Workday MCP to an Azure VM
# Usage: ./deploy-azure.sh [resource-group] [vm-name]
set -e

RG="${1:-workday-mcp-rg}"
VM="${2:-workday-mcp-vm}"
LOCATION="eastus"
VM_SIZE="Standard_B2s"
IMAGE="Canonical:0001-com-ubuntu-server-jammy:22_04-lts-gen2:latest"
ADMIN_USER="azureuser"

echo "=== Workday MCP — Azure Deployment ==="
echo "Resource Group: $RG"
echo "VM Name:        $VM"
echo "Location:       $LOCATION"
echo "Size:           $VM_SIZE"
echo ""

# Check Azure CLI
if ! command -v az &> /dev/null; then
    echo "Error: Azure CLI (az) is not installed."
    echo "Install: https://learn.microsoft.com/en-us/cli/azure/install-azure-cli"
    exit 1
fi

# Check login
az account show > /dev/null 2>&1 || { echo "Run 'az login' first."; exit 1; }

echo "Step 1: Creating resource group..."
az group create --name "$RG" --location "$LOCATION" --output none

echo "Step 2: Creating VM ($VM_SIZE)..."
VM_IP=$(az vm create \
    --resource-group "$RG" \
    --name "$VM" \
    --image "$IMAGE" \
    --size "$VM_SIZE" \
    --admin-username "$ADMIN_USER" \
    --generate-ssh-keys \
    --public-ip-sku Standard \
    --os-disk-size-gb 32 \
    --output tsv --query publicIpAddress)

echo "VM IP: $VM_IP"

echo "Step 3: Opening ports (8000=MCP, 6080=noVNC)..."
az vm open-port --resource-group "$RG" --name "$VM" --port 8000 --priority 1010 --output none
az vm open-port --resource-group "$RG" --name "$VM" --port 6080 --priority 1020 --output none

echo "Step 4: Installing Docker on VM..."
az vm run-command invoke \
    --resource-group "$RG" \
    --name "$VM" \
    --command-id RunShellScript \
    --scripts '
        curl -fsSL https://get.docker.com | sh
        usermod -aG docker '"$ADMIN_USER"'
        systemctl enable docker
        apt-get install -y docker-compose-plugin
    ' --output none

echo "Step 5: Copying project to VM..."
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
scp -o StrictHostKeyChecking=no -r \
    "$PROJECT_DIR"/*.py \
    "$PROJECT_DIR"/requirements.txt \
    "$PROJECT_DIR"/Dockerfile \
    "$PROJECT_DIR"/docker-compose.yml \
    "$PROJECT_DIR"/.env.example \
    "$ADMIN_USER@$VM_IP:/tmp/workday-mcp/"

# Copy .claude directory if it exists
if [ -d "$PROJECT_DIR/.claude" ]; then
    scp -o StrictHostKeyChecking=no -r "$PROJECT_DIR/.claude" "$ADMIN_USER@$VM_IP:/tmp/workday-mcp/"
fi

echo "Step 6: Building and starting containers..."
ssh -o StrictHostKeyChecking=no "$ADMIN_USER@$VM_IP" '
    sudo mkdir -p /opt/workday-mcp
    sudo cp -r /tmp/workday-mcp/* /opt/workday-mcp/
    [ -d /tmp/workday-mcp/.claude ] && sudo cp -r /tmp/workday-mcp/.claude /opt/workday-mcp/
    cd /opt/workday-mcp
    sudo cp .env.example .env
    sudo docker compose up -d --build
'

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "MCP Server:  http://$VM_IP:8000/mcp"
echo "noVNC (SSO): http://$VM_IP:6080/vnc.html"
echo ""
echo "Next steps:"
echo "  1. Open http://$VM_IP:6080/vnc.html in your browser"
echo "  2. Tell Claude: 'Sign me in to Workday' — complete SSO in the noVNC window"
echo "  3. Add this to your Claude Desktop/Code config:"
echo ""
echo '  "workday": {'
echo '    "url": "http://'"$VM_IP"':8000/mcp"'
echo '  }'
echo ""
echo "Security: Restrict ports 8000 and 6080 to your corporate IP range:"
echo "  az network nsg rule update --resource-group $RG --nsg-name ${VM}NSG \\"
echo "    --name open-port-8000 --source-address-prefixes YOUR_CORP_IP_RANGE"
echo ""
echo "To stop:   ssh $ADMIN_USER@$VM_IP 'cd /opt/workday-mcp && sudo docker compose down'"
echo "To logs:   ssh $ADMIN_USER@$VM_IP 'cd /opt/workday-mcp && sudo docker compose logs -f mcp'"
echo "To delete: az group delete --name $RG --yes --no-wait"
