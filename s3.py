#!/usr/bin/env python2
import boto3
import botocore
import collections
import re
import shutil
import signal
import sys
import threading
import urlparse

class MessageHeader(collections.namedtuple('MessageHeader_', ['status_code', 'status_info'])):
    def __str__(self):
        return '{} {}'.format(self.status_code, self.status_info)

    @staticmethod
    def parse(line):
        status_code, status_info = line.split(' ', 1)
        return MessageHeader(int(status_code), status_info)

class MessageHeaders:
    CAPABILITIES = MessageHeader(100, 'Capabilities')
    STATUS = MessageHeader(102, 'Status')
    URI_FAILURE = MessageHeader(400, 'URI Failure')
    GENERAL_FAILURE = MessageHeader(401, 'General Failure')
    URI_START = MessageHeader(200, 'URI Start')
    URI_DONE = MessageHeader(201, 'URI Done')
    URI_ACQUIRE = MessageHeader(600, 'URI Acquire')

# TODO: Handle RFC 822 more robustly. Would use email module, but it always adds Content-Type and MIME-Version
class Message(collections.namedtuple('Message_', ['header', 'fields'])):
    @staticmethod
    def parse_lines(lines):
        return Message(MessageHeader.parse(lines[0]), dict(re.split(': *', field, 1) for field in lines[1:]))

    def __str__(self):
        lines = [str(self.header)]
        lines.extend('{}: {}'.format(name, value) for name, value in self.fields.iteritems())
        lines.append('\n')
        return '\n'.join(lines)

Pipes = collections.namedtuple('Pipes', ['input', 'output'])

class AptMethod(collections.namedtuple('AptMethod_', ['pipes'])):
    def send(self, message):
        self.pipes.output.write(str(message))
        self.pipes.output.flush()

    def run(self):
        try:
            self.send_capabilities()

            # TODO: Use a proper executor. concurrent.futures has them, but it's only in Python 3.2+.
            threads = []
            error_info = None

            lines = []
            while error_info is None:
                line = sys.stdin.readline()
                if not line:
                    for thread in threads:
                        thread.join()
                    break
                line = line.rstrip('\n')
                if line:
                    lines.append(line)
                elif lines:
                    message = Message.parse_lines(lines)
                    lines = []
                    def handle_message():
                        try:
                            self.handle_message(message)
                        except:
                            error_info = sys.exc_info()
                    thread = threading.Thread(target=handle_message)
                    threads.append(thread)
                    thread.start()
                else:
                    pass

            if error_info is not None:
                raise error_info[1], None, error_info[2]
        except Exception as ex:
            self.send(Message(MessageHeaders.GENERAL_FAILURE, {'Message': ex}))
            raise

class S3AptMethod(AptMethod):
    def send_capabilities(self):
        self.send(Message(MessageHeaders.CAPABILITIES, {
            'Pipeline': 'true',
            'Single-Instance': 'yes',
        }))

    def handle_message(self, message):
        if message.header.status_code == MessageHeaders.URI_ACQUIRE.status_code:
            uri = message.fields['URI']
            uri_parts = urlparse.urlparse(uri)

            s3_config = {}
            try:
                at_index = uri_parts.netloc.index('@')
            except ValueError:
                authority = uri_parts.netloc
            else:
                user_parts = uri_parts.netloc[:at_index].split(':', 1)
                try:
                    s3_config['aws_access_key_id'], s3_config['aws_secret_access_key'] = user_parts
                except ValueError:
                    raise Exception('Access key and secret are specified improperly in the URL')
                authority = uri_parts.authority[at_index + 1:]
            s3_config['endpoint_url'] = 'https://{}/'.format(authority)
            try:
                s3 = boto3.resource('s3', **s3_config)
            except:


            virtual_host_match = re.match('(.*).s3(?:-[^.]*)?.amazonaws.com', uri_parts.host)
            if virtual_host_match:
                bucket = virtual_host_match.group(1)
                key = uri_parts.path[1:]
            else:
                _, bucket, key = uri_parts.path.split('/', 2)
            s3_object = s3.Bucket(bucket).Object(key)

            self.send(Message(MessageHeaders.STATUS, {
                'Message': 'Requesting {}/{}'.format(bucket, key),
                'URI': uri,
            }))
            try:
                s3_request = {}
                try:
                    last_modified = message.fields['Last-Modified']
                except KeyError:
                    pass
                else:
                    s3_request['IfModifiedSince'] = last_modified
                s3_response = s3_object.get(**s3_request)
            except botocore.exceptions.ClientError as error:
                if error.response['Error']['Code'] == '304':
                    self.send(Message(MessageHeaders.URI_DONE, {
                        'Filename': message.fields['Filename'],
                        'IMS-Hit': 'true',
                        'URI': uri,
                    }))
                else:
                    self.send(Message(MessageHeaders.URI_FAILURE, {
                        'Message': error.response['Error']['Message'],
                        'URI': uri,
                    }))
            else:
                self.send(Message(MessageHeaders.URI_START, {
                    'Last-Modified': s3_response['LastModified'].isoformat(),
                    'Size': s3_response['ContentLength'],
                    'URI': uri,
                }))
                with open(message.fields['Filename'], 'wb') as f:
                    shutil.copyfileobj(s3_response['Body'], f)
                self.send(Message(MessageHeaders.URI_DONE, {
                    'Filename': message.fields['Filename'],
                    'Last-Modified': s3_response['LastModified'].isoformat(),
                    'Size': s3_response['ContentLength'],
                    'URI': uri,
                }))

if __name__ == '__main__':
    def signal_handler(signal, frame):
        sys.exit(0)
    signal.signal(signal.SIGINT, signal_handler)

    pipes = Pipes(sys.stdin, sys.stdout)
    S3AptMethod(pipes).run()
