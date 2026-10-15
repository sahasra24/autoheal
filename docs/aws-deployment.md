# Deploying to AWS EKS (optional)

> **Cost warning:** EKS charges for the control plane (~$0.10/h), plus 2 × t3.medium nodes, NAT gateway, the ALB and EBS volumes. Expect a few dollars per day. **Delete everything when you're done** (see the end of this page). The core project needs none of this; minikube or kind cover every scenario.

Tools: AWS CLI v2 (configured), `eksctl`, `kubectl`, `helm`, Docker.

## 1. Create the cluster (~15 min)

```bash
eksctl create cluster -f deploy/eks/cluster.yaml
kubectl apply -f deploy/eks/storageclass.yaml
kubectl get nodes
```

The config sets up OIDC (IRSA), the VPC CNI **with NetworkPolicy enforcement**, the EBS CSI driver, metrics-server (for the HPA), and a 2 to 4 node managed node group in private subnets.

## 2. Install the AWS Load Balancer Controller

```bash
helm repo add eks https://aws.github.io/eks-charts && helm repo update eks
helm upgrade --install aws-load-balancer-controller eks/aws-load-balancer-controller -n kube-system \
  --set clusterName=autoheal \
  --set serviceAccount.create=false \
  --set serviceAccount.name=aws-load-balancer-controller
```

## 3. Monitoring (optional but recommended)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace -f deploy/k8s/kube-prometheus-stack-values.yaml --wait
```

## 4. Push images to ECR

```bash
export AWS_REGION=us-east-1
export REGISTRY=$(aws sts get-caller-identity --query Account --output text).dkr.ecr.$AWS_REGION.amazonaws.com
export TAG=$(git rev-parse --short HEAD)
aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY
for s in gateway tasks control; do
  aws ecr create-repository --repository-name autoheal-$s --image-tag-mutability IMMUTABLE \
      --image-scanning-configuration scanOnPush=true 2>/dev/null || true
  docker build -f services/$s/Dockerfile -t $REGISTRY/autoheal-$s:$TAG --build-arg VERSION=$TAG .
  docker push $REGISTRY/autoheal-$s:$TAG
done
```

## 5. Deploy

```bash
kubectl apply -f deploy/k8s/namespace.yaml
helm upgrade --install autoheal helm/autoheal -n autoheal -f helm/autoheal/values-eks.yaml \
  --set image.registry=$REGISTRY --set image.tag=$TAG --atomic --wait --timeout 10m

kubectl -n autoheal get ingress autoheal-gateway   # ADDRESS = public ALB hostname (takes ~2 min)
curl http://<alb-hostname>/api/v1/cluster/overview
```

`values-eks.yaml` turns on the ALB ingress (`target-type: ip`, health check on `/health/ready`), trusts `X-Forwarded-For` from the ALB, makes Redis persistent on gp3, raises HPA ceilings, and enables the ServiceMonitor, PrometheusRule and dashboard.

To demo self-healing on EKS, run the same scenarios through the control service: `kubectl -n autoheal port-forward svc/autoheal-control 8090:80`. Also try draining a node, `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`, and watch the PDBs keep at least one gateway and one tasks pod serving while pods move.

## 6. Keyless CD from GitHub Actions

`.github/workflows/cd.yml` deploys every green `main` build. It authenticates with **GitHub OIDC**, so no AWS access keys are stored in GitHub.

1. Create the GitHub OIDC identity provider in IAM (once per account):
   ```bash
   aws iam create-open-id-connect-provider --url https://token.actions.githubusercontent.com \
     --client-id-list sts.amazonaws.com
   ```
2. Create a role from `deploy/eks/github-oidc-trust.json` (replace `<ACCOUNT_ID>`, `<GITHUB_USER>`, `<REPO>`). Attach a policy allowing ECR push (`ecr:*` on `autoheal-*` repositories, `ecr:GetAuthorizationToken`) and `eks:DescribeCluster`.
3. Map the role into the cluster:
   ```bash
   eksctl create iamidentitymapping --cluster autoheal --region $AWS_REGION \
     --arn arn:aws:iam::<ACCOUNT_ID>:role/<ROLE_NAME> --username github-deployer --group system:masters
   ```
   (For least privilege, bind a namespaced Role instead of `system:masters`.)
4. In the GitHub repo settings, add variables `AWS_DEPLOY_ENABLED=true`, `AWS_REGION` and `EKS_CLUSTER=autoheal`, and the secret `AWS_DEPLOY_ROLE_ARN`. Optionally add required reviewers to the `production` environment for a manual approval gate.

The workflow builds and pushes immutable SHA-tagged images, then runs `helm upgrade --atomic`. If the new pods never pass their probes, the release rolls back automatically.

## 7. Tear down

```bash
helm uninstall autoheal -n autoheal                 # removes the ALB (created by the Ingress)
helm uninstall kube-prometheus-stack -n monitoring
kubectl delete pvc --all -n autoheal                # releases EBS volumes
eksctl delete cluster -f deploy/eks/cluster.yaml
for s in gateway tasks control; do aws ecr delete-repository --repository-name autoheal-$s --force; done
```

Uninstall the chart **before** deleting the cluster. Otherwise the ALB and its security groups can be orphaned and keep billing.
