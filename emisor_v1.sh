#!/bin/bash
echo -ne "\033]0;EMISOR V1\007"
exec python3 /home/difusor01/Aplicaciones/99_Multimedia/RTMP/Linea_Stream/sistema_emision/app.py
