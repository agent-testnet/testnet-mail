#!/usr/bin/env bash
#
# Deploy testnet-mail to AWS.
#
# Usage:
#   bash deploy/aws-deploy.sh deploy          # Provision + deploy
#   bash deploy/aws-deploy.sh teardown        # Soft teardown (keeps EIP + data volume)
#   bash deploy/aws-deploy.sh teardown --full # Full teardown (destroys everything)
#   bash deploy/aws-deploy.sh status          # Instance state + health check
#   bash deploy/aws-deploy.sh ssh             # Interactive SSH session
#   bash deploy/aws-deploy.sh redeploy        # Re-upload code + restart
#   bash deploy/aws-deploy.sh restart         # Restart services only
#   bash deploy/aws-deploy.sh logs            # Tail service logs
#
# Required env vars (deploy only):
#   SERVER_URL    Testnet control plane URL
#   NODE_NAME     Node name in nodes.yaml
#   NODE_SECRET   Shared secret from nodes.yaml
#   MAIL_DOMAIN   Primary email domain (e.g. gmail.com)
#
# Prerequisites:
#   - AWS CLI configured (aws sts get-caller-identity)
#   - python3, rsync
#
set -euo pipefail

# ── Log helpers ───────────────────────────────────────────────────────────────

info()  { printf "\033[1;34m==>\033[0m %s\n" "$*" >&2; }
warn()  { printf "\033[1;33mWARN:\033[0m %s\n" "$*" >&2; }
err()   { printf "\033[1;31mERROR:\033[0m %s\n" "$*" >&2; exit 1; }

# ── Directory variables ───────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
STATE_FILE="${STATE_FILE:-${SCRIPT_DIR}/.aws-state.json}"

# ── Configuration ─────────────────────────────────────────────────────────────

REGION="${AWS_REGION:-$(aws configure get region 2>/dev/null || echo "eu-west-1")}"
export AWS_DEFAULT_REGION="$REGION"

INSTANCE_TYPE="${INSTANCE_TYPE:-t3a.micro}"
UBUNTU_OWNER="099720109477"

STACK_TAG="testnet-stack"
STACK_VALUE="${STACK_VALUE:-testnet-mail}"
STACK_PREFIX="${STACK_PREFIX:-testnet-mail}"

KEY_NAME="${STACK_PREFIX}"
KEY_FILE="${SCRIPT_DIR}/.aws-${STACK_PREFIX}-key.pem"

DATA_VOLUME_SIZE=10
DATA_MOUNT_POINT="/opt/testnet-mail"
ROLE="mail"

# ── State helpers ─────────────────────────────────────────────────────────────

save_state() {
    local key="$1" value="$2"
    [ -f "$STATE_FILE" ] || echo '{}' > "$STATE_FILE"
    local tmp="${STATE_FILE}.tmp"
    python3 -c "
import json
with open('$STATE_FILE') as f: state = json.load(f)
state['$key'] = '$value'
with open('$tmp', 'w') as f: json.dump(state, f, indent=2)
"
    mv "$tmp" "$STATE_FILE"
}

load_state() {
    local key="$1"
    [ -f "$STATE_FILE" ] || { echo ""; return; }
    python3 -c "
import json
with open('$STATE_FILE') as f: state = json.load(f)
print(state.get('$key', ''))
"
}

# ── Tag helper ────────────────────────────────────────────────────────────────

tag_spec() {
    echo "ResourceType=$1,Tags=[{Key=${STACK_TAG},Value=${STACK_VALUE}},{Key=Name,Value=${STACK_PREFIX}-$2}]"
}

# ── SSH helpers ───────────────────────────────────────────────────────────────

wait_for_ssh() {
    local ip="$1" key="$2" max_attempts=40 attempt=0
    ssh-keygen -R "$ip" 2>/dev/null || true
    info "Waiting for SSH on ${ip}..."
    while [ $attempt -lt $max_attempts ]; do
        if ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=5 -o BatchMode=yes \
            -i "$key" "ubuntu@${ip}" "echo ready" >/dev/null 2>&1; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 5
    done
    err "SSH to ${ip} timed out after $((max_attempts * 5))s"
}

remote_exec() {
    local ip="$1" key="$2"; shift 2
    ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$key" "ubuntu@${ip}" "$@"
}

remote_copy() {
    local key="$1" src="$2" dest="$3"
    scp -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$key" "$src" "$dest"
}

# ── EIP helpers ───────────────────────────────────────────────────────────────

ensure_eip() {
    EIP_ALLOC=$(load_state eip_alloc_id)
    EIP_PUBLIC=$(load_state eip_public_ip)
    if [ -n "$EIP_ALLOC" ]; then
        if aws ec2 describe-addresses --allocation-ids "$EIP_ALLOC" &>/dev/null; then
            info "Reusing Elastic IP $EIP_PUBLIC ($EIP_ALLOC)"
            return
        fi
        warn "EIP $EIP_ALLOC no longer exists, allocating new one"
    fi
    info "Allocating Elastic IP..."
    local alloc_json
    alloc_json=$(aws ec2 allocate-address \
        --domain vpc \
        --tag-specifications "$(tag_spec elastic-ip eip)" \
        --output json)
    EIP_ALLOC=$(echo "$alloc_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['AllocationId'])")
    EIP_PUBLIC=$(echo "$alloc_json" | python3 -c "import sys,json; print(json.load(sys.stdin)['PublicIp'])")
    save_state eip_alloc_id "$EIP_ALLOC"
    save_state eip_public_ip "$EIP_PUBLIC"
    info "Allocated Elastic IP $EIP_PUBLIC ($EIP_ALLOC)"
}

associate_eip() {
    local instance_id="$1"
    info "Associating EIP $EIP_PUBLIC with $instance_id..."
    aws ec2 associate-address \
        --allocation-id "$EIP_ALLOC" \
        --instance-id "$instance_id" >/dev/null
}

# ── EBS volume helpers ────────────────────────────────────────────────────────

ensure_volume() {
    local role="$1" size="$2" az="$3"
    local vol_id
    vol_id=$(load_state "vol_${role}")
    if [ -n "$vol_id" ]; then
        if aws ec2 describe-volumes --volume-ids "$vol_id" &>/dev/null; then
            info "Reusing data volume $vol_id for $role"
            echo "$vol_id"
            return
        fi
        warn "Volume $vol_id no longer exists, creating new one"
    fi
    info "Creating ${size} GiB GP3 data volume in $az..."
    vol_id=$(aws ec2 create-volume \
        --volume-type gp3 \
        --size "$size" \
        --availability-zone "$az" \
        --tag-specifications "$(tag_spec volume "${role}-data")" \
        --query 'VolumeId' \
        --output text)
    save_state "vol_${role}" "$vol_id"
    save_state az "$az"
    info "Created data volume $vol_id"
    echo "$vol_id"
}

attach_and_mount_volume() {
    local vol_id="$1" instance_id="$2" ip="$3" key="$4" mount_point="$5"
    info "Attaching volume $vol_id to $instance_id..."
    aws ec2 attach-volume \
        --volume-id "$vol_id" \
        --instance-id "$instance_id" \
        --device /dev/xvdf >/dev/null
    info "Waiting for volume to attach..."
    aws ec2 wait volume-in-use --volume-ids "$vol_id"
    sleep 5

    info "Mounting volume at $mount_point..."
    remote_exec "$ip" "$key" "sudo bash -s" << MOUNT
set -euo pipefail
if [ -b /dev/nvme1n1 ]; then
    DEV=/dev/nvme1n1
elif [ -b /dev/xvdf ]; then
    DEV=/dev/xvdf
else
    echo "ERROR: No data volume device found" >&2
    exit 1
fi
if ! blkid "\$DEV" &>/dev/null; then
    echo "Formatting \$DEV as ext4..."
    mkfs.ext4 -q "\$DEV"
fi
mkdir -p "$mount_point"
mount "\$DEV" "$mount_point"
if ! grep -q "$mount_point" /etc/fstab; then
    UUID=\$(blkid -s UUID -o value "\$DEV")
    echo "UUID=\$UUID $mount_point ext4 defaults,nofail 0 2" >> /etc/fstab
fi
MOUNT
    info "Volume mounted at $mount_point"
}

detach_volume() {
    local role="$1"
    local vol_id
    vol_id=$(load_state "vol_${role}")
    [ -z "$vol_id" ] && return
    local vol_state
    vol_state=$(aws ec2 describe-volumes --volume-ids "$vol_id" \
        --query 'Volumes[0].State' --output text 2>/dev/null || echo "unknown")
    if [ "$vol_state" = "in-use" ]; then
        info "Detaching volume $vol_id..."
        aws ec2 detach-volume --volume-id "$vol_id" >/dev/null
        aws ec2 wait volume-available --volume-ids "$vol_id"
        info "Volume detached"
    fi
}

delete_volume() {
    local role="$1"
    local vol_id
    vol_id=$(load_state "vol_${role}")
    [ -z "$vol_id" ] && return
    info "Deleting volume $vol_id..."
    aws ec2 delete-volume --volume-id "$vol_id" 2>/dev/null || true
    info "Volume deleted"
}

# ── provision_host ────────────────────────────────────────────────────────────

provision_host() {
    local ip="$1" key="$2"
    info "Installing prerequisites on $ip..."
    remote_exec "$ip" "$key" "sudo bash -s" << 'PROVISION'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "  [1/4] Updating packages..."
apt-get update -qq
apt-get upgrade -y -qq

echo "  [2/4] Installing Docker..."
if ! command -v docker &>/dev/null; then
    apt-get install -y -qq ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
fi

echo "  [3/4] Installing nginx..."
if ! command -v nginx &>/dev/null; then
    apt-get install -y -qq nginx
    systemctl enable nginx
fi

apt-get install -y -qq gettext-base jq

echo "  [4/4] Installing testnet-toolkit..."
if [ ! -f /usr/local/bin/testnet-toolkit ]; then
    TOOLKIT_URL=$(curl -fsSL https://api.github.com/repos/agent-testnet/agent-testnet/releases/latest \
        | jq -r '.assets[] | select(.name | test("toolkit.*linux.*amd64")) | .browser_download_url' \
        | head -1)
    if [ -n "$TOOLKIT_URL" ] && [ "$TOOLKIT_URL" != "null" ]; then
        curl -fsSL "$TOOLKIT_URL" -o /usr/local/bin/testnet-toolkit
        chmod +x /usr/local/bin/testnet-toolkit
        echo "    testnet-toolkit installed from release"
    else
        echo "    WARNING: Could not find testnet-toolkit release. Install manually."
    fi
fi

echo "  Provisioning complete."
PROVISION
}

# ── rsync_and_deploy ──────────────────────────────────────────────────────────

rsync_and_deploy() {
    local ip="$1" key="$2"
    info "Syncing repo to instance..."
    rsync -az --delete \
        --exclude '.git' \
        --exclude '.DS_Store' \
        --exclude 'deploy/.aws-*' \
        --exclude 'config/' \
        --exclude 'backups/' \
        -e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i $key" \
        "$PROJECT_DIR/" "ubuntu@${ip}:/tmp/testnet-mail/"

    info "Running deploy.sh on instance..."
    remote_exec "$ip" "$key" "sudo bash -s" << DEPLOY
set -euo pipefail
export SERVER_URL='${SERVER_URL}'
export NODE_NAME='${NODE_NAME}'
export NODE_SECRET='${NODE_SECRET}'
export MAIL_DOMAIN='${MAIL_DOMAIN}'
cd /tmp/testnet-mail
bash scripts/deploy.sh
rm -rf /tmp/testnet-mail
DEPLOY
}

# ── do_deploy ─────────────────────────────────────────────────────────────────

do_deploy() {
    : "${SERVER_URL:?SERVER_URL is required}"
    : "${NODE_NAME:?NODE_NAME is required}"
    : "${NODE_SECRET:?NODE_SECRET is required}"
    : "${MAIL_DOMAIN:?MAIL_DOMAIN is required}"

    local existing_instance
    existing_instance=$(load_state "instance_${ROLE}")
    if [ -n "$existing_instance" ]; then
        local state
        state=$(aws ec2 describe-instances \
            --instance-ids "$existing_instance" \
            --query 'Reservations[0].Instances[0].State.Name' \
            --output text 2>/dev/null || echo "terminated")
        if [ "$state" = "running" ]; then
            err "Existing instance $existing_instance is running. Use 'redeploy' to update code, or 'teardown' first."
        fi
        info "Previous instance $existing_instance is $state, provisioning new one..."
    fi

    info "Deploying testnet-mail to AWS"
    info "Region: $REGION | Instance: $INSTANCE_TYPE"

    # Key pair
    if aws ec2 describe-key-pairs --key-names "$KEY_NAME" &>/dev/null; then
        info "Key pair '$KEY_NAME' already exists"
    else
        info "Creating key pair '$KEY_NAME'..."
        aws ec2 create-key-pair \
            --key-name "$KEY_NAME" \
            --tag-specifications "$(tag_spec key-pair key)" \
            --query 'KeyMaterial' \
            --output text > "$KEY_FILE"
        chmod 600 "$KEY_FILE"
        info "Private key saved to $KEY_FILE"
    fi
    [ -f "$KEY_FILE" ] || err "Key file $KEY_FILE not found. Delete the key pair and re-run: aws ec2 delete-key-pair --key-name $KEY_NAME"
    save_state key_name "$KEY_NAME"
    save_state key_file "$KEY_FILE"

    # Security group (default VPC)
    local vpc_id sg_id
    vpc_id=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
        --query 'Vpcs[0].VpcId' --output text)

    sg_id=$(aws ec2 describe-security-groups \
        --filters "Name=group-name,Values=${STACK_PREFIX}-sg" "Name=vpc-id,Values=$vpc_id" \
        --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

    if [ "$sg_id" = "None" ] || [ -z "$sg_id" ]; then
        info "Creating security group '${STACK_PREFIX}-sg'..."
        sg_id=$(aws ec2 create-security-group \
            --group-name "${STACK_PREFIX}-sg" \
            --description "Testnet mail server" \
            --vpc-id "$vpc_id" \
            --tag-specifications "$(tag_spec security-group sg)" \
            --query 'GroupId' --output text)
        aws ec2 authorize-security-group-ingress --group-id "$sg_id" \
            --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null
        aws ec2 authorize-security-group-ingress --group-id "$sg_id" \
            --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null
        aws ec2 authorize-security-group-ingress --group-id "$sg_id" \
            --protocol tcp --port 25 --cidr 0.0.0.0/0 >/dev/null
        info "Created $sg_id with SSH, HTTPS, SMTP ingress"
    else
        info "Reusing security group $sg_id"
    fi
    save_state sg_id "$sg_id"

    # AMI
    info "Looking up Ubuntu 22.04 AMI..."
    local ami_id
    ami_id=$(aws ec2 describe-images \
        --owners "$UBUNTU_OWNER" \
        --filters \
            "Name=name,Values=ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*" \
            "Name=state,Values=available" \
        --query 'sort_by(Images, &CreationDate)[-1].ImageId' \
        --output text)
    info "AMI: $ami_id"

    # Elastic IP
    ensure_eip

    # Launch instance (pin to existing AZ if we have a volume)
    local placement_args=()
    local saved_az
    saved_az=$(load_state az)
    if [ -n "$saved_az" ]; then
        placement_args=(--placement "AvailabilityZone=$saved_az")
        info "Pinning instance to $saved_az (existing data volume)"
    fi

    info "Launching $INSTANCE_TYPE instance..."
    local instance_id
    instance_id=$(aws ec2 run-instances \
        --image-id "$ami_id" \
        --instance-type "$INSTANCE_TYPE" \
        --key-name "$KEY_NAME" \
        --security-group-ids "$sg_id" \
        --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":8,"VolumeType":"gp3"}}]' \
        --tag-specifications "$(tag_spec instance mail)" \
        "${placement_args[@]+"${placement_args[@]}"}" \
        --query 'Instances[0].InstanceId' \
        --output text)
    save_state "instance_${ROLE}" "$instance_id"
    info "Instance: $instance_id"

    # Wait for running
    info "Waiting for instance to start..."
    aws ec2 wait instance-running --instance-ids "$instance_id"

    # Associate EIP
    associate_eip "$instance_id"
    save_state "ip_${ROLE}" "$EIP_PUBLIC"

    # Get AZ from instance (for volume placement)
    local instance_az
    instance_az=$(aws ec2 describe-instances \
        --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' \
        --output text)

    # Data volume
    local vol_id
    vol_id=$(ensure_volume "$ROLE" "$DATA_VOLUME_SIZE" "$instance_az")

    # Wait for status checks before SSH
    info "Waiting for instance status checks..."
    aws ec2 wait instance-status-ok --instance-ids "$instance_id"

    # SSH, mount, provision, deploy
    wait_for_ssh "$EIP_PUBLIC" "$KEY_FILE"
    attach_and_mount_volume "$vol_id" "$instance_id" "$EIP_PUBLIC" "$KEY_FILE" "$DATA_MOUNT_POINT"
    provision_host "$EIP_PUBLIC" "$KEY_FILE"
    rsync_and_deploy "$EIP_PUBLIC" "$KEY_FILE"

    info "Deploy complete"
    echo ""
    echo "  Instance:   $instance_id"
    echo "  Elastic IP: $EIP_PUBLIC"
    echo "  Region:     $REGION"
    echo "  Volume:     $vol_id (${DATA_VOLUME_SIZE} GiB at $DATA_MOUNT_POINT)"
    echo ""
    echo "  ssh:        $0 ssh"
    echo "  status:     $0 status"
    echo "  logs:       $0 logs"
    echo "  redeploy:   $0 redeploy"
    echo "  teardown:   $0 teardown"
    echo ""
}

# ── do_teardown ───────────────────────────────────────────────────────────────

do_teardown() {
    local full="${1:-}"
    local instance_id
    instance_id=$(load_state "instance_${ROLE}")

    if [ -z "$instance_id" ] && [ "$full" != "--full" ]; then
        if [ -f "$STATE_FILE" ]; then
            warn "No instance found (previous soft teardown?). Use 'teardown --full' to release EIP and delete volume."
            return
        fi
        err "No deployment found in state file"
    fi

    if [ -n "$instance_id" ]; then
        detach_volume "$ROLE"

        info "Terminating instance $instance_id..."
        aws ec2 terminate-instances --instance-ids "$instance_id" >/dev/null 2>&1 || true
        info "Waiting for termination..."
        aws ec2 wait instance-terminated --instance-ids "$instance_id" 2>/dev/null || true
        info "Instance terminated"
    fi

    # Delete security group (retry -- deletion can lag behind termination)
    local sg_id
    sg_id=$(load_state sg_id)
    if [ -n "$sg_id" ]; then
        info "Deleting security group $sg_id..."
        for _ in 1 2 3 4 5; do
            if aws ec2 delete-security-group --group-id "$sg_id" 2>/dev/null; then
                info "Security group deleted"
                break
            fi
            sleep 5
        done
    fi

    # Delete key pair
    local key_name
    key_name=$(load_state key_name)
    if [ -n "$key_name" ]; then
        info "Deleting key pair '$key_name'..."
        aws ec2 delete-key-pair --key-name "$key_name" 2>/dev/null || true
        rm -f "$KEY_FILE"
        info "Key pair deleted"
    fi

    if [ "$full" = "--full" ]; then
        local eip_alloc
        eip_alloc=$(load_state eip_alloc_id)
        if [ -n "$eip_alloc" ]; then
            info "Releasing Elastic IP $eip_alloc..."
            aws ec2 release-address --allocation-id "$eip_alloc" 2>/dev/null || true
            info "Elastic IP released"
        fi
        delete_volume "$ROLE"
        rm -f "$STATE_FILE"
        info "Full teardown complete. All AWS resources removed."
    else
        # Soft teardown: prune state to persistent keys only
        info "Pruning state to persistent keys (EIP + volume)..."
        local eip_alloc_id eip_public_ip vol_id az
        eip_alloc_id=$(load_state eip_alloc_id)
        eip_public_ip=$(load_state eip_public_ip)
        vol_id=$(load_state "vol_${ROLE}")
        az=$(load_state az)
        rm -f "$STATE_FILE"
        [ -n "$eip_alloc_id" ]  && save_state eip_alloc_id "$eip_alloc_id"
        [ -n "$eip_public_ip" ] && save_state eip_public_ip "$eip_public_ip"
        [ -n "$vol_id" ]        && save_state "vol_${ROLE}" "$vol_id"
        [ -n "$az" ]            && save_state az "$az"
        info "Soft teardown complete. EIP and data volume preserved."
    fi
}

# ── do_status ─────────────────────────────────────────────────────────────────

do_status() {
    local instance_id ip eip_alloc eip_public vol_id
    instance_id=$(load_state "instance_${ROLE}")
    eip_alloc=$(load_state eip_alloc_id)
    eip_public=$(load_state eip_public_ip)
    vol_id=$(load_state "vol_${ROLE}")
    ip="${eip_public:-}"

    echo "Testnet Mail Deployment"
    echo "  Region:     $REGION"
    echo "  Elastic IP: ${eip_public:-none} (${eip_alloc:-none})"
    echo "  Volume:     ${vol_id:-none}"

    if [ -z "$instance_id" ]; then
        echo "  Instance:   none (torn down)"
        return
    fi

    local state
    state=$(aws ec2 describe-instances \
        --instance-ids "$instance_id" \
        --query 'Reservations[0].Instances[0].State.Name' \
        --output text 2>/dev/null || echo "unknown")

    echo "  Instance:   $instance_id ($state)"

    if [ "$state" = "running" ] && [ -n "$ip" ]; then
        echo ""
        echo "  Health check:"
        if remote_exec "$ip" "$KEY_FILE" \
            "curl -sf http://127.0.0.1:8080/?_task=login >/dev/null 2>&1 && echo '    Roundcube: OK' || echo '    Roundcube: UNREACHABLE'" 2>/dev/null; then
            true
        else
            echo "    Could not connect via SSH"
        fi
        echo ""
        echo "  Containers:"
        remote_exec "$ip" "$KEY_FILE" \
            "sudo docker compose -f /opt/testnet-mail/docker-compose.yml ps --format 'table {{.Name}}\t{{.Status}}'" 2>/dev/null \
            || echo "    Could not query containers"
    fi
}

# ── do_ssh ────────────────────────────────────────────────────────────────────

do_ssh() {
    local ip
    ip=$(load_state eip_public_ip)
    [ -z "$ip" ] && err "No Elastic IP in state -- is the service deployed?"
    if [ $# -eq 0 ]; then
        info "Connecting to $ip..."
        exec ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -i "$KEY_FILE" "ubuntu@${ip}"
    else
        [ "${1:-}" = "--" ] && shift
        remote_exec "$ip" "$KEY_FILE" "$@"
    fi
}

# ── do_redeploy ───────────────────────────────────────────────────────────────

do_redeploy() {
    : "${SERVER_URL:?SERVER_URL is required}"
    : "${NODE_NAME:?NODE_NAME is required}"
    : "${NODE_SECRET:?NODE_SECRET is required}"
    : "${MAIL_DOMAIN:?MAIL_DOMAIN is required}"

    local ip instance_id
    instance_id=$(load_state "instance_${ROLE}")
    ip=$(load_state eip_public_ip)
    [ -z "$instance_id" ] || [ -z "$ip" ] && err "No running deployment found. Run 'deploy' first."
    rsync_and_deploy "$ip" "$KEY_FILE"
    info "Redeploy complete"
}

# ── do_restart ────────────────────────────────────────────────────────────────

do_restart() {
    local ip
    ip=$(load_state eip_public_ip)
    [ -z "$ip" ] && err "No running deployment found. Run 'deploy' first."
    info "Restarting services on $ip..."
    remote_exec "$ip" "$KEY_FILE" "sudo docker compose -f /opt/testnet-mail/docker-compose.yml restart"
    info "Services restarted"
}

# ── do_logs ───────────────────────────────────────────────────────────────────

do_logs() {
    local ip
    ip=$(load_state eip_public_ip)
    [ -z "$ip" ] && err "No running deployment found. Run 'deploy' first."
    info "Tailing logs on $ip (Ctrl+C to stop)..."
    remote_exec "$ip" "$KEY_FILE" "sudo docker compose -f /opt/testnet-mail/docker-compose.yml logs -f --tail 100"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────

ACTION="${1:-}"
case "$ACTION" in
    deploy)   do_deploy ;;
    teardown) do_teardown "${2:-}" ;;
    status)   do_status ;;
    ssh)      shift; do_ssh "$@" ;;
    redeploy) do_redeploy ;;
    restart)  do_restart ;;
    logs)     do_logs ;;
    "")       err "Usage: $0 <deploy|teardown [--full]|status|ssh|redeploy|restart|logs>" ;;
    *)        err "Unknown action: $ACTION" ;;
esac
