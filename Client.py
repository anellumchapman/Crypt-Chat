import json
import socket
import select
import sys
import queue
from Crypto.Hash import SHA512
from Crypto.PublicKey import RSA
import binascii
from src import message as message
from src import Encryption as crypto
from src import Helper


# TODO: Implement a nicer UI. (qt)

class Client:
    def __init__(self):
        self.sock = socket.socket()
        self.client_name = ''
        self.server_port = 5000
        self.server_address = ''
        self.running = False
        print("Generating secure key...")
        self.client_key = RSA.generate(4096)
        self.server_key = None
        self.user_keys = {}   # username : publicKey
        self.groups = {}      # groupname : [String]
        # TODO: Add logs to prepare for separate channels of communication.
        self.group_logs = {}  # groupname : [String]
        self.user_logs = {}   # username : [String]

    def load_config(self):
        try:
            json_data = json.load(open("./config.json"))
            self.server_address = json_data["server-address"]
            self.server_port = json_data["port"]
            return True
        except FileNotFoundError:
            print("Config file not found!")
            return False
        except ValueError:
            return False

    def start(self):
        self.running = True
        connected = False
        if self.load_config():
            try:
                self.sock.connect((self.server_address, self.server_port))
                connected = True
                print("Connected to {}:{} via config".format(self.server_address, self.server_port))
            except:
                print("Config not valid!")
        while not connected:
            try:
                self.server_address = input("Enter Server IP: ")
                self.sock.connect((self.server_address, self.server_port))
                connected = True
                print("Successfully Connected to {}!".format(self.server_address))
            except ConnectionRefusedError:
                print("Invalid IP or server did not respond.")
        self.run()

    def run(self):
        request_count = 0
        waiting_for_key = []
        waiting_for_users = {}  # expected response id : (group, message)
        message_queue = queue.Queue()
        inputs = [self.sock, sys.stdin]
        while self.running:
            inputs_ready, _, _ = select.select(inputs, [], [])
            for s in inputs_ready:
                if s == sys.stdin:  # TODO: break down into functions
                    user_input = sys.stdin.readline()
                    if user_input.startswith('/'):
                        if user_input.lower().startswith('/msg') or user_input.lower().startswith('/gmsg'):
                            split_user_input = user_input.split(' ')
                            to = split_user_input[1]
                            text = " ".join(split_user_input[2:]).strip('\n')
                            if split_user_input[0].startswith('/gmsg'):
                                json_message = {'type': 'group-message', 'group': to,
                                                'message': text, 'from': self.client_name}
                            else:
                                json_message = message.Message(to, text, self.client_name).data
                            message_queue.put(json_message)
                        elif user_input.lower().startswith('/register'):
                            split_user_input = user_input.strip().split(' ')
                            usn = split_user_input[1]
                            self.client_name = usn
                            pswhash = hash_password(split_user_input[2])
                            pswhash = crypto.encrypt_message(pswhash, self.server_key)

                            request = message.Request(message.REGISTER_REQUEST, [usn, pswhash]).data
                            message_queue.put(request)
                        elif user_input.lower().startswith('/login'):
                            split_user_input = user_input.strip().split(' ')
                            try:
                                usn, psw = split_user_input[1:]
                            except ValueError:
                                print("Invalid command! /login <username> <password>")
                            passhash = hash_password(psw)
                            passhash = crypto.encrypt_message(passhash, self.server_key)
                            psw = None
                            rq = message.Request(message.AUTH_REQUEST, [usn, passhash])
                            passhash = None
                            self.client_name = usn
                            #  print(rq.to_json())
                            message_queue.put(rq.data)
                        elif user_input.lower() == '/exit\n':
                            self.stop()
                            sys.exit(0)

                if s == self.sock:
                    received = s.recv(4096).decode('utf-8')
                    if len(received) > 0:
                        results = Helper.clean_json(received)
                        for r in results:  # received multiple json strings in one buffer, clean_json strips them into
                                           # a list of json strings
                            self.handle_sock(r, message_queue, waiting_for_key, waiting_for_users)


            while not message_queue.empty():
                msg = message_queue.get_nowait()
                if msg['type'] == 'group-message' and msg.get('to') is None:
                    group = msg['group']
                    # request user list
                    waiting_for_users[request_count] = (group, msg)
                    msg = {'type': 'request', 'request': 'group-list',
                                       'group': group, 'id': request_count, 'from': self.client_name}
                    request_count += 1
                elif msg['type'] == 'message' or msg['type'] == 'group-message':
                    if msg['to'] not in self.user_keys.keys():
                        waiting_for_key.append(msg)  # put it in the waiting pile
                        # send a request for public key
                        # print('Don\'t have the public key, sending a request for it.')
                        msg = message.Request('pubkey', [msg['to'], ]).data
                    else:
                        msg['message'] = crypto.encrypt_message(msg['message'].encode('utf-8'), self.user_keys[msg['to']])
                if isinstance(msg, dict):
                    data = json.dumps(msg)
                else:
                    data = msg
                data = data.encode('utf-8')
                # print(json.dumps(msg))
                self.sock.send(data)

    def handle_sock(self, received, message_queue, waiting_for_key, waiting_for_users):
        json_data = json.loads(received)
        if json_data['type'] == 'pubkey':
            if json_data.get('tag') is None:  # Server public key
                print('Received Handshake request')
                self.server_key = RSA.importKey(json_data['key'])
                msg = {'type': 'pubkey', 'key': self.client_key.publickey()
                    .exportKey('PEM')
                    .decode('utf-8')}
                self.sock.send(json.dumps(msg).encode('utf-8'))
                print('Performing Handshake...')
            else:
                user = json_data['tag']
                # print('Server returned public key for {}.'.format(user))
                self.user_keys[user] = RSA.importKey(json_data['message'])
                for msg in waiting_for_key:
                    if user == msg['to']:
                        message_queue.put(msg)  # put back in message queue to be sent
                        # print('Resending message: {} \nto {}.'.format(msg['message'], msg['to']))
                        waiting_for_key.remove(msg)  # remove from waiting
        elif json_data['type'] == 'message':
            msg = crypto.decrypt_message(json_data['message'], self.client_key)
            text = '<{}>: {}'.format(json_data['from'], msg)
            log = self.user_logs.get(json_data['from'])
            if log is None:
                self.user_logs[json_data['from']] = log = []
            log.append(text)
            print(text)
        elif json_data['type'] == 'group-message':
            msg = crypto.decrypt_message(json_data['message'], self.client_key)
            text = '<{}>({}): {}'.format(json_data['from'], json_data['group'], msg)
            log = self.group_logs.get(json_data['group'])
            if log is None:
                log = []
            log.append(text)
            print(text)
        elif json_data['type'] == 'error':
            print("ERROR: " + json_data['message'])
        elif json_data['type'] == 'InvalidUserError':  # remove from waiting
            print("Server doesn't have public key for user, sorry")
            for msg in waiting_for_key:
                if msg['to'] == json_data['message']:
                    waiting_for_key.remove(msg)
        elif json_data['type'] == 'auth-error':
            print(json_data['message'])
            self.client_name = ''
        elif json_data['type'] == message.SUCCESS:
            print(json_data['message'])
        elif json_data['type'] == 'group-list':
            group, msg = waiting_for_users[json_data['id']]
            users = json_data['message']
            self.groups[group] = users
            # print('received group list: ', users)
            for u in users:
                if self.client_name != u:
                    msg['to'] = u
                    message_queue.put(msg.copy())
            waiting_for_users[json_data['id']] = None
        elif json_data['type'] == 'shutdown':
            print('Server shut down!')
            self.sock.close()
            sys.exit(0)

    def stop(self):
        try:
            data = {'type': 'logout'}
            data = json.dumps(data)
            self.sock.send(data.encode('utf-8'))
            self.running = False
            self.sock.close()
        except:
            pass

        sys.exit(0)



def hash_password(pwd):
    pswhash = SHA512.new(pwd.encode('utf-8')).digest()
    pswhash = binascii.hexlify(pswhash)
    return pswhash


if __name__ == "__main__":
    c = Client()
    # c.stop()
    try:
        c.start()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print("ERROR: " + e)
    finally:
        c.stop()
        print("Client Closed!")

