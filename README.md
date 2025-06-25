python -m grpc_tools.protoc -I./pronity-frontend/protos --python_out=./livekit-service/rox/generated/protos --pyi_out=./livekit-service/rox/generated/protos --grpc_python_out=./livekit-service/rox/generated/protos ./pronity-frontend/protos/interaction.proto

