apiVersion: v1
kind: Service
metadata:
  name: mlflow
  namespace: mlops
spec:
  type: ClusterIP
  selector:
    app: mlops-mlflow
  ports:
    - name: http
      port: 5000
      targetPort: http
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: mlflow
  namespace: mlops
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: mlops-mlflow
  template:
    metadata:
      labels:
        app: mlops-mlflow
    spec:
      serviceAccountName: mlflow
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        runAsNonRoot: true
      nodeSelector:
        eks.amazonaws.com/nodegroup: system
      containers:
        - name: mlflow
          image: ${MLFLOW_IMAGE}
          imagePullPolicy: IfNotPresent
          # MLflow 3.6 initializes ./mlruns while resolving a logged model for Registry.
          # /app is root-owned; /tmp is writable by the non-root container user.
          workingDir: /tmp
          env:
            - name: MLFLOW_DB_USER
              value: mlflow
            - name: MLFLOW_DB_NAME
              value: mlflow
            - name: MLFLOW_DB_HOST
              value: postgres.mlops.svc.cluster.local
            - name: MLFLOW_DB_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: mlflow-db
                  key: password
            - name: MLFLOW_ARTIFACTS_DESTINATION
              value: s3://${MODEL_BUCKET}/mlflow/
            - name: MLFLOW_SERVER_ALLOWED_HOSTS
              value: "mlflow.mlops.svc.cluster.local:5000,10.20.*,localhost:*,127.0.0.1:*"
            - name: AWS_DEFAULT_REGION
              value: ap-southeast-1
          ports:
            - name: http
              containerPort: 5000
          startupProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 5
            failureThreshold: 30
          readinessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: http
            periodSeconds: 20
          resources:
            requests:
              cpu: 200m
              memory: 512Mi
            limits:
              cpu: "1"
              memory: 1Gi
