# livekit-service/README.md
1. pip install -r requirements.txt
2. cd rox
3. set .env in rox folder
4. python main.py download-files
5. cd ..
6. python3 -m grpc_tools.protoc --python_out=rox/generated/protos --grpc_python_out=rox/generated/protos --proto_path=rox/protos rox/protos/interaction.proto
7. cd rox
8. python main.py

