apiVersion: batch/v1
kind: Job
metadata:
  name: mlops-training-${RUN_SUFFIX}
  namespace: mlops
  labels:
    app: mlops-training
spec:
  backoffLimit: 2
  activeDeadlineSeconds: 7200
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app: mlops-training
    spec:
      restartPolicy: Never
      serviceAccountName: training
      securityContext:
        fsGroup: 10001
      nodeSelector:
        mlops.aws/workload: training
        karpenter.sh/capacity-type: spot
      tolerations:
        - key: mlops.aws/workload
          operator: Equal
          value: training
          effect: NoSchedule
      containers:
        - name: training
          image: ${TRAINING_IMAGE}
          imagePullPolicy: IfNotPresent
          command: [sh, -ec]
          args:
            - >-
              mlops-cloud fetch --bucket "$$DATA_BUCKET" --config configs/training.yaml --data-dir /app/dataset
              && mlops-train train --config configs/training.yaml --input-dir /app/dataset
              --output-dir /app/artifacts/training --track --mlflow-config configs/mlflow.yaml
              --tracking-uri "$$MLFLOW_TRACKING_URI"
          env:
            - name: DATA_BUCKET
              value: ${DATA_BUCKET}
            - name: MLFLOW_TRACKING_URI
              value: http://mlflow.mlops.svc.cluster.local:5000
            - name: AWS_DEFAULT_REGION
              value: ap-southeast-1
          resources:
            requests:
              cpu: 1200m
              memory: 2Gi
              ephemeral-storage: 1Gi
            limits:
              cpu: "2"
              memory: 3Gi
              ephemeral-storage: 4Gi
          volumeMounts:
            - name: dataset
              mountPath: /app/dataset
            - name: results
              mountPath: /app/artifacts
      volumes:
        - name: dataset
          emptyDir:
            sizeLimit: 4Gi
        - name: results
          emptyDir:
            sizeLimit: 1Gi
